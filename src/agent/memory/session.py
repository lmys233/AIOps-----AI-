"""会话记忆：多轮历史 + **指代消解** + 澄清记账。

★ 指代消解要解决什么：

没有它：「order 的 P99 涨了」→「**它**的错误率呢」→ 第二轮槽位里 service 为空 →
      落到澄清，**用户被问了一个自己刚说过的东西**（体验最差的那类交互）。

有了它：第二轮把"它"绑到**上一轮提到的实体**，`slots.service = order`。

★★ 三条不可退让的规则（宁可问一句，不要瞎猜）：

1. **只看最近一轮"提到了实体"的历史**。更早的提及不作数 —— 对话里刚说过的才算"它"。
2. **那一轮里出现多个候选 → 不解析**，把候选带出去让编排层澄清。
   猜错的代词比不解析更危险：它会让 Agent 在**错的实体**上取证，
   然后给出一份看起来合理、实际针对别的服务的结论。
3. **跨会话不解析**（工作记忆是会话级的，TTL 24h 到期即失效），
   且**提及距今超过 `max_lookback_turns` 轮 → 不解析**（引用早已失效，"它"指什么没人知道）。

★ 消解结果必须**可解释**（`note` 里写明"代词 ← 第几轮 ← 哪个实体"）：
  这条信息要进响应与日志 —— 否则"代词消解错了"将是一个查不出来的问题。
"""

from __future__ import annotations
from ..observed.context import session_key

from dataclasses import dataclass

import logging
from datetime import datetime, timezone
from typing import Any, Final, Iterable, Mapping, Optional, Sequence

from ..state.backend import encode_json
from .stores import ConversationTurn, WorkingMemory
from .stores.records import (TURN_ANSWER_MAX_ROWS, TURN_ANSWER_SUMMARY_MAX,
                            TURN_ANSWER_VALUE_MAX, TURN_STEPS_MAX, TURN_STEP_TEXT_MAX,
                            TURN_TOOLS_MAX, TURN_TOOL_TEXT_MAX)

__all__ = [
    "ReferenceResolution",
    "SessionMemory",
    "PRONOUNS",
    "DEFAULT_LOOKBACK_TURNS",
    "ASKED_GAPS_CEILING",
]

logger = logging.getLogger("agent.memory.session")
from .answer_snapshot import snapshot_of


def _tool_fields(tools: Optional[Sequence[Mapping[str, Any]]]
                 ) -> tuple[tuple[str, str, str, str, bool, str], ...]:
    """把这一轮的工具调用收成**有界**的记录（前端恢复工具卡 + 下一轮给模型看）。

    ★ 末尾那个 `call_id` 是**执行过程时间线**与这条记录对齐的钥匙（同名调用可以并发）。
    """
    out: list[tuple[str, str, str, str, bool, str]] = []
    for item in (tools or ()):
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        out.append((name, str(item.get("title") or "").strip(),
                    str(item.get("params") or "")[:TURN_TOOL_TEXT_MAX],
                    str(item.get("result") or "")[:TURN_TOOL_TEXT_MAX],
                    bool(item.get("ok", True)),
                    str(item.get("call_id") or "")))
        if len(out) >= TURN_TOOLS_MAX:
            break
    return tuple(out)


def _steps_fields(steps: Optional[Sequence[Mapping[str, Any]]]
                  ) -> tuple[tuple[str, str], ...]:
    """把这一轮的**执行过程时间线**收成有界记录：`((event, payload_json), …)`。

    ★ 只做"保形 + 有界 + 转 JSON"，**不做文案** —— 每一行长什么样由前端
      `summarizeEvent` 那一处决定（服务端另写一份必然漂移）。
    ★ 工具那一步只留**位置**（`call_id` + 名字），卡片内容在 `tools` 里，前端按 `call_id` 对齐。
    """
    out: list[tuple[str, str]] = []
    for item in (steps or ()):
        if not isinstance(item, Mapping):
            continue
        event = str(item.get("event") or "").strip()
        if not event:
            continue
        payload = ({"call_id": str(item.get("call_id") or ""), "name": str(item.get("name") or "")}
                   if event == "tool" else dict(item.get("data") or {}))
        out.append((event, encode_json(payload)[:TURN_STEP_TEXT_MAX]))
        if len(out) >= TURN_STEPS_MAX:
            break
    return tuple(out)


def _answer_fields(answer: Optional[Mapping[str, Any]]
                   ) -> tuple[str, tuple[tuple[str, str], ...], str]:
    """把答案原文收成**有界**的三件（总结 / 明细行 / 展示形态）。

    ★ 形状不对就当没有（**不猜**：宁可这一轮恢复时只显示摘要，也不编一份答案出来）。
    """
    if not isinstance(answer, Mapping):
        return "", (), ""
    summary = str(answer.get("summary") or "").strip()[:TURN_ANSWER_SUMMARY_MAX]
    kind = str(answer.get("details_kind") or "").strip()
    rows: list[tuple[str, str]] = []
    for item in (answer.get("details") or ()):
        if not isinstance(item, Mapping):
            continue
        value = str(item.get("value") or "").strip()
        if not value:
            continue
        rows.append((str(item.get("label") or "").strip(),
                     value[:TURN_ANSWER_VALUE_MAX]))
        if len(rows) >= TURN_ANSWER_MAX_ROWS:
            break
    return summary, tuple(rows), kind

#: 代词/指示词（中文为主）。★ 这是**语言数据**，但规模小且与业务无关，
#: 放代码里可读性更好；要做多语言时再外置成 YAML。
PRONOUNS: Final[tuple[str, ...]] = (
    "它俩", "它们", "它", "他", "她",
    "刚才那个", "上面那个", "刚才那个服务", "前面那个",
    "这个服务", "那个服务", "该服务", "此服务", "该组件", "这个组件",
    "这个", "那个", "前者", "后者",
)

#: ★★ **省略式追问** —— 这句话没有代词，但显然在问"刚才那件事"。
#:   用户实测：「怎么解决？」紧跟在一份诊断结论后面，却被要求"补充目标服务" ——
#:   而目标就在上一轮里（`inventory`）。代词表抓不到它，因为**它一个指代词都没有**。
ELLIPTICAL_FOLLOWUPS: Final[tuple[str, ...]] = (
    "怎么解决", "怎么办", "如何解决", "如何修复", "怎么修", "怎么处理", "如何处理",
    "怎么排查", "啥原因", "什么原因", "下一步", "然后呢", "接着说", "继续说", "还有吗",
)

#: 超过这么多轮之前的提及不再用于消解
DEFAULT_LOOKBACK_TURNS: Final[int] = 5

#: 澄清记账的**兜底上界系数**：`asked_limit`（每轮预算）× 本系数 = 同一会话最多攒几个
#: "问过的缺口"。★ 它是**兜底**（保证不会无限问下去），真正的预算由澄清层裁决 ——
#: 以前这个 10 是散在表达式里的裸数字，读者无从知道它是契约值还是手滑。
ASKED_GAPS_CEILING: Final[int] = 10


@dataclass(frozen=True)
class ReferenceResolution:
    """代词消解的结论（**永远有返回**，`resolved=False` 时给出原因与候选）。"""

    text: str
    pronoun: Optional[str] = None
    resolved: bool = False
    service: Optional[str] = None
    source_turn: Optional[int] = None
    candidates: tuple[str, ...] = ()
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "pronoun": self.pronoun, "resolved": self.resolved,
                "service": self.service, "source_turn": self.source_turn,
                "candidates": list(self.candidates), "note": self.note}


@dataclass
class SessionMemory:
    """会话级记忆（工作记忆之上的一层薄封装，职责单一）。"""

    working: WorkingMemory
    max_turns: int = 20
    lookback_turns: int = DEFAULT_LOOKBACK_TURNS

    # ------------------------------------------------------------------
    async def remember_turn(self, session_id: str, *, user_message: str,
                            response_digest: str = "",
                            answer: Optional[Mapping[str, Any]] = None,
                            service_names: Iterable[str] = ()) -> ConversationTurn:
        """记一轮对话（同时把"提到过哪些服务"存下来 —— 消解靠它）。

        ★★ `answer` 是**这一轮给用户看过的答案原文**（`explanation.answer` 那份：
          `summary` / `details` / `details_kind`）—— 用户实测上报："刷新后对话显示就变了……
          回答原文为什么不给看？"。近层是"原文"那一层，答案必须落在这里，且**有界**
          （总结 ≤600 字符、明细 ≤16 行、每行值 ≤300 字符）。
        """
        history = await self.working.turns(session_key(session_id), limit=self.max_turns)
        summary, rows, kind = _answer_fields(answer)
        turn = ConversationTurn(
            turn=(history[-1].turn + 1) if history else 1,
            user_message=str(user_message or ""),
            response_digest=str(response_digest or "")[:200],
            service_names=tuple(dict.fromkeys(str(s) for s in service_names if str(s).strip())),
            at=datetime.now(timezone.utc).isoformat(),
            answer_summary=summary, answer_rows=rows, answer_kind=kind,
            answer_advisor_status=str((answer or {}).get("advisor_status") or ""),
            answer_snapshot=snapshot_of(answer),
        )
        await self.working.append_turn(session_key(session_id), turn)
        return turn

    async def attach_answer(self, session_id: str, answer: Optional[Mapping[str, Any]],
                            tools: Optional[Sequence[Mapping[str, Any]]] = None,
                            steps: Optional[Sequence[Mapping[str, Any]]] = None) -> bool:
        """把**这一轮给用户看过的答案原文**补到刚写下的那一轮上。

        ★ 为什么是"补写"：轮次记账发生在**编排收口**（`Orchestrator._finish`），
          而答案是**渲染层**的产物（`explanation.answer`）—— 那时答案还没生成。
          于是先记轮次、渲染完再补这一笔；`False` 表示"没补上"（调用方要**留痕**，
          否则刷新后只剩摘要，而用户不知道为什么）。
        ★ `steps` 是这一轮的**执行过程时间线**（工具卡 + 非工具环节的有序记录）——
          用户实测要求"刷新后依旧保留 AIOps 执行的过程"，所以它与答案一起补写。
        """
        summary, rows, kind = _answer_fields(answer)
        tool_records = _tool_fields(tools)
        step_records = _steps_fields(steps)
        if not summary and not rows and not tool_records and not step_records:
            return False
        history = await self.working.turns(session_key(session_id), limit=1)
        if not history:
            return False
        last = history[-1]
        same_view=(answer or {}).get('view_id')==(last.answer_snapshot or {}).get('view_id')
        unchanged=not (answer or {}).get('view_version') or same_view
        if unchanged and last.answer_summary and last.answer_rows and last.tools and last.steps:
            return True                              # 已经是补过的那一份（幂等，别重复写）
        return await self.working.replace_last_turn(session_key(session_id), ConversationTurn(
            turn=last.turn, user_message=last.user_message,
            response_digest=last.response_digest, service_names=last.service_names,
            at=last.at, answer_summary=summary, answer_rows=rows, answer_kind=kind,
            answer_advisor_status=str((answer or {}).get("advisor_status") or ""),
            answer_snapshot=snapshot_of(answer),
            tools=tool_records or last.tools, steps=step_records or last.steps))

    async def turns(self, session_id: str, *, limit: int = 0) -> list[ConversationTurn]:
        """读**近期轮次**（按时间正序，最近的在后）；`limit<=0` 用会话上限 `max_turns`。

        ★ 这是恢复会话时读正文的**唯一接口**：只召回近层原文；远层走索引，要旧内容时再查。
        """
        size = int(limit) if int(limit) > 0 else self.max_turns
        return await self.working.turns(session_key(session_id), limit=size)

    async def history(self, session_id: str) -> list[ConversationTurn]:
        """近期轮次（`turns` 的旧名字，语义完全一致；保留给既有调用方）。"""
        return await self.turns(session_id)

    # ------------------------------------------------------------------
    async def resolve_reference(self, session_id: str, text: str, *,
                                need_service: bool = True) -> ReferenceResolution:
        """把 `text` 里的代词绑到最近提到的服务。

        `need_service` 表示当前槽位**是否缺** service；不缺就不需要消解
        （代词只是重复指代，不会改变结论）。
        """
        clean = str(text or "")
        pronoun = next((word for word in PRONOUNS if word in clean), None)
        ellipsis = next((word for word in ELLIPTICAL_FOLLOWUPS if word in clean), None)
        if pronoun is None and ellipsis is None:
            return ReferenceResolution(text=clean, note="文本里没有代词/指示词，无需消解")
        if not need_service:
            return ReferenceResolution(
                text=clean, pronoun=pronoun,
                note=f"出现代词「{pronoun or ellipsis}」，但本次槽位里已有服务 —— 不需要消解",
            )
        # ★★ 省略式追问按**同一个口径**消解（最近提到服务的那些轮次 + 失效期 + 多候选不猜）
        kind = f"代词「{pronoun}」" if pronoun else f"省略式追问「{ellipsis}」"

        try:
            history = await self.working.turns(session_key(session_id), limit=self.max_turns)
        except Exception as exc:  # noqa: BLE001 —— 记忆不可用不该让整轮失败
            logger.warning("会话历史不可用，跳过指代消解：%s", exc)
            return ReferenceResolution(text=clean, pronoun=pronoun,
                                       note=f"会话历史不可用（{type(exc).__name__}），未消解")

        if not history:
            return ReferenceResolution(
                text=clean, pronoun=pronoun,
                note=f"{kind}，但会话里没有任何历史 —— 不解析（宁可问一句）",
            )

        # ---- 规则 1：只看最近一轮**提到了实体**的历史 ----
        latest = next((turn for turn in reversed(history) if turn.service_names), None)
        if latest is None:
            return ReferenceResolution(
                text=clean, pronoun=pronoun,
                note=f"{kind}，但历史里没有任何轮次提到过服务 —— 不解析",
            )

        # ---- 规则 3：提及太久远 → 不解析 ----
        age = history[-1].turn - latest.turn
        if age > self.lookback_turns:
            return ReferenceResolution(
                text=clean, pronoun=pronoun,
                note=(f"{kind}最近一次提及在第 {latest.turn} 轮"
                      f"（距今 {age} 轮 > {self.lookback_turns}）—— 引用已失效，不解析"),
            )

        # ---- 规则 2：同一轮里多个候选 → 不猜 ----
        if len(latest.service_names) > 1:
            return ReferenceResolution(
                text=clean, pronoun=pronoun, candidates=tuple(latest.service_names),
                note=(f"{kind} → 第 {latest.turn} 轮提到 "
                      f"{list(latest.service_names)} **多个候选** —— 不猜，交给澄清"),
            )

        service = latest.service_names[0]
        return ReferenceResolution(
            text=clean, pronoun=pronoun, resolved=True, service=service,
            source_turn=latest.turn,
            candidates=(service,),
            note=(f"{kind} ← 第 {latest.turn} 轮 ← service={service}"
                  f"（依据：那一轮是最近一次提到服务的历史）"),
        )

    # ------------------------------------------------------------------
    #  澄清记账（给编排层的"同一个问题只问一次"用）
    # ------------------------------------------------------------------
    async def mark_asked(self, session_id: str, gap: str) -> None:
        """记下"这个缺口已经问过用户了"。"""
        await self.working.mark_asked_gap(session_key(session_id), gap)

    async def asked_gaps(self, session_id: str) -> set[str]:
        """已经问过的缺口。★ 编排层据此实现"同一缺口只问一次"。"""
        return await self.working.asked_gaps(session_key(session_id))

    async def should_ask(self, session_id: str, gap: str, *, asked_limit: int = 1) -> bool:
        """这个缺口还该不该问？"""
        asked = await self.asked_gaps(session_id)
        if gap in asked:
            logger.info("缺口 %s 已经问过用户，不再重复问（会话 %s）", gap, session_id)
            return False
        return len(asked) < max(1, asked_limit) * ASKED_GAPS_CEILING

    # ------------------------------------------------------------------
    #  会话级状态（★ 任务台账用它 —— "我在等你补什么"必须活过这一轮）
    # ------------------------------------------------------------------
    async def set_state(self, session_id: str, state: Mapping[str, Any]) -> None:
        """整份写入会话状态（**读-改-写由调用方负责**，见 `api/tasks.py`）。"""
        await self.working.set_state(session_key(session_id), state)

    async def get_state(self, session_id: str) -> dict[str, Any]:
        """读会话状态（没有就是空 dict）。"""
        return await self.working.get_state(session_key(session_id))

    async def clear(self, session_id: str) -> None:
        """删掉该会话的近期记忆（删会话时一并清）；记忆不可用时抛出，由调用方如实说明。"""
        await self.working.clear(session_key(session_id))
