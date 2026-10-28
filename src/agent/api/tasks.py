"""会话任务台账：把"我正在等你补什么"记成**会话级状态**。

★ 为什么需要它（用户 2026-10-01 指示）：

> ① "反问后，用户会在**原本的对话框内**发送新的对话，AIOps 通过读取用户新对话，
>    根据内容获取到缺失数据"
> ② "如果用户在反问后不给出缺失信息，而是**询问新的事情**，不能影响新事情的处理……
>    可以把之前的任务**搁置**……当用户说『先前缺失的我现在给你』，AI 能**联系历史**，
>    重新完成之前的任务"

原先只有 `asked_gaps`（"这个缺口**问过**了"）—— 它回答不了"**在等什么**"，
于是反问之后那一轮就断了：用户下一句若没走 `/v1/chat/resume`，上一件事就再也接不上。

★ 三条口径：

  · **只记事实**：原话、在等哪几个槽位、问了什么、属于哪次 incident —— 不记"意图猜测"；
  · **落在会话状态里**（`session.set_state`）：重启/多实例下不丢（装配 Redis 时它就在 Redis 里）；
  · **判据写窄**：宁可把这句当"新任务"，也不要把用户的新问题吞掉（见 `answers_parked`）。
"""

from __future__ import annotations

import logging
import re
import time

#: ★ "这个意图用人话怎么说"属于**意图层的事实**，表在 `intent/augment.py`（只有一份）。
#:   这里再导出，于是 `from .tasks import INTENT_PHRASE` 照旧可用。
from ..common.enums import ChatStatus
from ..intent.augment import INTENT_PHRASE
from ..remediation.plans import class_of_title
from typing import Any, Final, Iterable, Mapping, Optional, Sequence

__all__ = ["PARKED_KEY", "RECENT_KEY", "SLOT_GAPS", "PLAN_GAP", "load_parked", "park",
           "clear_parked", "remember_recent", "load_recent", "describe_recent",
           "answers_parked", "describe_parked", "prepare_run_message",
           "sync_parked_task", "entities_of_body", "plans_of_body", "degraded_sink"]

logger = logging.getLogger("agent.api.tasks")

#: 会话状态里存台账的键（`session.get_state/set_state` 是**整份替换**，所以必须读-改-写）
PARKED_KEY: Final[str] = "parked_task"

#: ★ **近记忆**（刚完成的任务）的键。销账**不是丢掉** —— 挪到这里，
#:   让"紧接着的追问"还能接上（实测：任务一销账，追问「那还有别的可能吗」就被拒答了）。
RECENT_KEY: Final[str] = "recent_task"

#: 近记忆的条数上限与**存活时间**：★ 太久远的大概率已被修复，不该再被召回
MAX_RECENT: Final[int] = 3
RECENT_TTL_SEC: Final[float] = 30 * 60.0

#: 台账只关心**槽位级**的缺口（不是插件的 gap 名）—— 它是"用户在补什么"的判据。
#: ★ `action` 是历史名（保留：旧台账里存的都是它，读到别认不出来）；
#:   写意图**没认出具体动作**时真正缺的不是"做什么动作"，而是"**用户在候选方案里选哪一条**"
#:   （候选由代码生成，见 `remediation.plans`）—— 那个槽位叫 `remediation`。
SLOT_GAPS: Final[tuple[str, ...]] = ("service", "env", "time_window", "action",
                                     "remediation")

#: ★ "**在等用户选方案**"这一件事的记账键（写意图的澄清问题带的是 `remediation` 槽位）。
#:   它与 `slots_missing` 分开记：后者按 `SLOT_GAPS` 派生（摘要的"缺什么"用它），
#:   而这一条是"还差一个选择"，**不是**缺一个槽位值。
PLAN_GAP: Final[str] = "remediation"

#: 待办栈的上限（用户可能连续搁置几件事；再多说明该清理了）
MAX_PARKED: Final[int] = 3

#: 显式接续词：用户明说"我在补之前那个"
_RESUME_WORDS: Final[tuple[str, ...]] = (
    "先前", "刚才", "之前", "上次", "那个", "缺失", "补上", "补充", "接着", "继续", "回到")

#: "新诉求"的信号词：出现这些就**不当**成补信息（否则用户的新问题会被旧任务吞掉）
from ..intent.continuation import _NEW_ASK_WORDS


#: 台账里意图**不可用**时（例如泛化健康询问走了 `OUT_OF_SCOPE` 兜底）的默认读法：
#: 用户给了服务名 = "帮我排查这个服务有没有问题"。
DEFAULT_RESUME_PHRASE: Final[str] = "排查这个服务的故障"


async def load_parked(session: Any, session_id: str,
                      degraded: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """读这本会话的**待办栈**（最近的在最前；没有 / 读不到 → 空列表，**不猜**）。

    ★ 为什么是一叠而不是一条（用户指示："可以把之前的任务搁置，完成新任务"）：
      用户可能连续搁置两件事（A 缺服务、B 缺时间窗），只留一条会让**前一条被覆盖**。
      上限 3 条：再多就说明该清理了，超出的**丢掉最旧的**并在响应里可见（不静默）。
    """
    if session is None or not session_id:
        return []
    state = await _state_of(session, session_id, degraded)
    items = state.get(PARKED_KEY)
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return []
    return [dict(item) for item in items if isinstance(item, Mapping)]


async def park(session: Any, session_id: str, *, message: str,
         tools: Sequence[Mapping[str, Any]] = (), incident_id: str,
               gaps: Iterable[str], questions: Sequence[Any],
               intent: str = "", entities: Sequence[str] = (),
               plans: Sequence[Any] = (),
               degraded: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """把"在等的任务"写进待办栈（**按原话 upsert**，不覆盖别的任务）。

    ★ 多记一份 `entities`（**已解析出的实体**：服务/环境/时间窗）。
      真机实测：只记用户原话时，台账里那条是「怎么解决？」—— 一个实体都没有，
      于是"接续"时重跑的消息里服务丢了（`service=None`），用户看到的还是"你在问哪个服务"。
      实体是**这一轮已经解析出来的事实**，不记下来就得每轮重解析一遍（而且解析不出来）。
    ★ 多记一份 `plans`（这一轮给出的**候选方案**摘要）与 `plan_gap`：
      "在等什么"如实写成"**在等用户选方案**"，用户下一句回「方案 2」时也认得出来。
    ★ `degraded`：落共享状态失败时的留痕入口（见 `_persist`）—— 失败不抛异常，本轮照常跑完。
    """
    gap_set = {str(name) for name in gaps}
    slots_missing = [name for name in SLOT_GAPS if name in gap_set]
    item = {
        "message": str(message or ""),
        "incident_id": str(incident_id or ""),
        "gaps": sorted(gap_set),
        "slots_missing": slots_missing,
        "questions": [_question_text(question) for question in questions][:3],
        "intent": str(intent or ""),
        "entities": [str(name) for name in entities if str(name).strip()],
    }
    # ★★ 记下这一轮**实际调过的工具**（下轮编织块里给模型看："上轮已查：…"）
    if tools:
        item["tools"] = [{"name": str(entry.get("name") or ""),
                          "title": str(entry.get("title") or ""),
                          "result": str(entry.get("result") or "")[:120],
                          "ok": bool(entry.get("ok", True))}
                         for entry in list(tools)[:12] if isinstance(entry, Mapping)]
    if PLAN_GAP in gap_set or "action" in gap_set:
        item["plan_gap"] = PLAN_GAP
        item["plans"] = [_plan_brief(plan) for plan in plans][:5]
    if session is None or not session_id:
        return [item]
    items = [old for old in await load_parked(session, session_id)
             if old.get("message") != item["message"]]
    items.insert(0, item)                      # 最近的在前
    items = items[:MAX_PARKED]
    await _save(session, session_id, items, degraded)
    return items


async def clear_parked(session: Any, session_id: str,
                       message: Optional[str] = None,
                       degraded: Optional[list[str]] = None) -> None:
    """销账：给了 `message` 就只销那一条（接续成功），否则清空整叠。

    ★ 销掉的那条**不是丢掉** —— 挪进**近记忆**（`remember_recent`），
      这样紧接着的追问（"那还有别的可能吗"）仍能接上。
    """
    if session is None or not session_id:
        return
    if message is None:
        await _save(session, session_id, [], degraded)
        return
    items = await load_parked(session, session_id)
    gone = next((old for old in items if old.get("message") == message), None)
    await _save(session, session_id,
                [old for old in items if old.get("message") != message], degraded)
    if gone is not None:
        await remember_recent(session, session_id, item=gone, degraded=degraded)


async def remember_recent(session: Any, session_id: str, *,
                          item: Mapping[str, Any],
                          now: Optional[float] = None,
                          degraded: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """把一条**已完成**的任务放进近记忆（按原话 upsert；最近的在前；有界）。

    ★ 存的是"这件事**做完了**"这个事实 + 它的实体（预筛要按实体匹配它）。
    ★ 条带上**状态与结果摘要**；写回前按**共享实体**连边。
    """
    stamp = float(now if now is not None else time.time())
    entry = dict(item)
    entry["finished_at"] = stamp
    entry["state"] = str(entry.get("state") or "done")
    if session is None or not session_id:
        return relate_entries([entry])
    items = [old for old in await load_recent(session, session_id, now=stamp)
             if old.get("message") != entry.get("message")]
    items.insert(0, entry)
    items = relate_entries(items[:MAX_RECENT])
    await _save_recent(session, session_id, items, degraded)
    return items


#: 一条任务最多记几条关联（有界：图不许长大到把提示词撑爆）
MAX_LINKS: Final[int] = 4


def relate_entries(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """★ 按**共享实体**给这批任务连边 → 带 `related` 的新条目。

    ★ 边是**可解释**的：每条关联写明"共享了哪个实体"（不是黑箱相似度）。
    ★ 只在**这一批里**连（= 同一会话、还在近记忆里的任务）—— 图有界，不需要遍历历史。
    """
    items = [dict(entry) for entry in entries]
    for item in items:
        mine = {str(name) for name in (item.get("entities") or ()) if str(name).strip()}
        related: list[dict[str, Any]] = []
        for other in items:
            if other is item or _same_task(item, other):
                continue
            shared = sorted(mine & {str(name) for name in (other.get("entities") or ())})
            if shared:
                related.append({"incident_id": str(other.get("incident_id") or ""),
                                "message": str(other.get("message") or "")[:60],
                                "shared": shared})
        item["related"] = related[:MAX_LINKS]
    return items


def _same_task(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """同一条任务（按 incident_id + 原话判；两个都空时按"不是同一条"处理）。"""
    if left.get("message") == right.get("message"):
        return True
    left_id, right_id = str(left.get("incident_id") or ""), str(right.get("incident_id") or "")
    return bool(left_id) and left_id == right_id


async def load_recent(session: Any, session_id: str, *,
                      now: Optional[float] = None,
                      degraded: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """读**近记忆**（过期的丢掉；读不到 → 空列表，**不猜**）。

    ★ 太久远的大概率已被修复，把三小时前的任务当"当前上下文"
      会误导追问（用户明确要的是"最近发生的尽量获取到，太久远的不召回"）。
    """
    if session is None or not session_id:
        return []
    state = await _state_of(session, session_id, degraded)
    raw = state.get(RECENT_KEY)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    stamp = float(now if now is not None else time.time())
    fresh = [dict(item) for item in raw if isinstance(item, Mapping)
             and stamp - float(item.get("finished_at") or 0.0) <= RECENT_TTL_SEC]
    return fresh[:MAX_RECENT]


def describe_recent(item: Mapping[str, Any]) -> str:
    """近记忆的人话（进 `degraded`：让"为什么这句接上了"可追溯）。"""
    if not item:
        return ""
    return (f"刚完成的任务（原话：「{str(item.get('message'))[:40]}」"
            f"，{_ago(float(item.get('finished_at') or 0.0))}前完成）")


def _ago(finished_at: float) -> str:
    seconds = max(0.0, time.time() - finished_at)
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    return f"{seconds / 60:.0f} 分钟"


async def _save_recent(session: Any, session_id: str,
                       items: Sequence[Mapping[str, Any]],
                       degraded: Optional[list[str]] = None) -> None:
    """整份写回近记忆（只动这一个键；读-改-写由调用方保证）。"""
    state = await _state_of(session, session_id)
    state[RECENT_KEY] = [dict(item) for item in items]
    await _persist(session, session_id, state, degraded)


def answers_parked(parked: Mapping[str, Any], message: str, *,
                   service_names: Sequence[str] = ()) -> tuple[bool, str]:
    """这句是在**补信息**，还是一个**新任务**？→ `(是否补信息, 依据)`

    ★ 为什么不用"跑一次意图识别再判断"：那会为**每一句**多花一次模型调用
      （而且把"要不要接续"押在模型输出上）。这里只用**确定性的**三条判据：

      ① 会话里有在等的任务（否则谈不上补）；
      ② 这句**含显式接续词**（"先前/刚才/补上/那个…"）**或**点到了在等的槽位
         （例如缺 service，而句子里出现了被观测清单里的服务名）；
      ③ 且**没有新诉求的信号词**（"为什么/变慢/多少/排查…"）—— 有的话就是新任务。

    ★ 补一条：台账里记的是"**在等用户选方案**"时，回「方案 2」/动作名同样是补信息
      （那是选方案，不是新问题）。
    """
    if not parked:
        return False, "本会话没有在等的任务"
    slots_missing = list(parked.get("slots_missing") or [])
    text = str(message or "").strip()
    if not text:
        return False, "空消息"
    if str(parked.get("plan_gap") or "") and _picks_a_plan(parked, text):
        return True, "在等的是「选方案」，这句正是在选（回「方案 N」或动作名）"
    from ..intent.augment import _explicit_request
    if (_explicit_request(text,None) or any(word in text for word in _NEW_ASK_WORDS)) and not any(
            word in text for word in ("先前", "刚才", "之前", "上次", "缺失", "补上")):
        return False, "句子里有新的诉求 → 当新任务（旧任务留在台账里）"
    if any(word in text for word in _RESUME_WORDS):
        return True, "显式接续词（用户明说在补之前那个）"
    filled = [name for name in slots_missing
              if name == "service" and _mentions_service(text, service_names)]
    if filled:
        return True, f"补上了在等的槽位：{filled}"
    return False, f"没补上在等的槽位 {slots_missing or '（无）'}"


def _plan_brief(plan: Any) -> dict[str, Any]:
    """台账里存一条方案的**摘要**（够把用户选的那条复原出来即可，不存整份）。

    ★ 存 `action_class` / `title` / 序号与四件事实 —— 下一轮用户回「方案 2」时，
      编排层按序号从这里复原（不必重算一批与上一轮可能不同的候选）。
    """
    keys = ("option_id", "title", "action_class", "risk_level", "rationale",
            "action", "mechanism", "evidence_refs", "expected", "blast_radius", "rollback",
            "requires_approval", "kind", "steps", "preconditions")
    if isinstance(plan, Mapping):
        return {key: plan.get(key) for key in keys}
    return {key: getattr(plan, key, "") for key in keys}


def _picks_a_plan(parked: Mapping[str, Any], text: str) -> bool:
    """这句明确选中了已展示的方案吗（序号 / 动作名 / 完整标题）？

    ★ 判据只看台账里存下的方案摘要 —— 台账不存整份方案对象（那是运行期产物，
      会话状态要能序列化）；用户的说法本来也就是围绕序号与动作名的。
    """
    from ..remediation.plans import plan_from_ledger

    return plan_from_ledger((parked,), text) is not None


def describe_parked(parked: Mapping[str, Any]) -> str:
    """台账的人话描述（进 `degraded`，让用户看得见"还有什么在等"）。"""
    if not parked:
        return ""
    if str(parked.get("plan_gap") or ""):
        titles = "、".join(str(item.get("title") or "") for item in
                           (parked.get("plans") or ()) if isinstance(item, Mapping))
        waiting = f"选方案（候选：{titles}）" if titles else "选方案"
    else:
        waiting = "、".join(parked.get("slots_missing") or parked.get("gaps") or ()) \
            or "（未记）"
    return (f"上一个任务还搁置着（原话：「{str(parked.get('message'))[:40]}」）——"
            f"我在等你补：{waiting}；补上我就接着查")


async def _save(session: Any, session_id: str, items: Sequence[Mapping[str, Any]],
                degraded: Optional[list[str]] = None) -> None:
    """整份写回（读-改-写由调用方保证；只动台账这一个键）。"""
    state = await _state_of(session, session_id)
    state[PARKED_KEY] = [dict(item) for item in items]
    await _persist(session, session_id, state, degraded)


async def _persist(session: Any, session_id: str, state: Mapping[str, Any],
                   degraded: Optional[list[str]]) -> None:
    """落会话状态：**失败不抛** —— 退回降级语义并把"没持久化"写进本次响应。

    ★ 契约要求共享状态落不下去时必须"退回进程内 + 在响应里说明"。
      以前这里让异常一路冒到端点：Redis 一挂，`/v1/chat` 直接 500 ——
      用户只看到"内部错误"，而真正的信息（这次什么都没记住）一个字都没有。
    """
    try:
        await session.set_state(session_id, state)
    except Exception as exc:  # noqa: BLE001 —— 状态落不下去是**降级**，不是这次请求失败
        note = (f"会话状态**未能持久化**（{type(exc).__name__}: {exc}）→ 本次按**进程内**"
                f"降级：这一轮的搁置任务/近记忆不落共享存储（重启即丢；读取路径读不到时"
                f"另有一条说明）")
        logger.warning("会话状态持久化失败（session=%s）：%s", session_id, note)
        if degraded is not None and note not in degraded:
            degraded.append(note)


def degraded_sink(body: Any) -> list[str]:
    """取响应体里的 `degraded` 列表（没有就建一个）—— 降级留痕必须进响应，不许只写日志。"""
    if not isinstance(body, dict):
        return []
    notes = body.get("degraded")
    if not isinstance(notes, list):
        notes = []
        body["degraded"] = notes
    return notes


from ..observed.names import service_names_of


async def prepare_run_message(session: Any, *, session_id: str, message: str,
                              service_names: Sequence[str],
                              degraded: list[str]) -> tuple[str, str]:
    """入口的**台账判定** → `(这次要跑的消息, 接续来源的原话)`。

    ★ 三种情形（用户指示的三种场景逐一对应）：
      · **在补信息** → 用「原话 + 这次的补充」重跑（语义与"手动重问一遍"完全一致，可解释）；
      · **问新事** → 照常跑这一句，旧任务**原样留在台账里**（并在 `degraded` 里提一句 ——
        既不静默丢掉、也不打断用户的新问题）；
      · **没有台账** → 原样跑。
    """
    parked = await load_parked(session, session_id, degraded)
    if not parked:
        return message, ""
    # ★ 在**整叠**里找"这句补上的是哪一件事"（最近的在前面）
    matched = next((item for item in parked
                    if answers_parked(item, message, service_names=service_names)[0]), None)
    if matched is None:
        why = answers_parked(parked[0], message, service_names=service_names)[1]
        degraded.append(f"{describe_parked(parked[0])}（本次按**新任务**处理：{why}）")
        return message, ""
    why = answers_parked(matched, message, service_names=service_names)[1]
    original = str(matched.get("message") or "")
    phrase = INTENT_PHRASE.get(str(matched.get("intent") or ""), "") or DEFAULT_RESUME_PHRASE
    degraded.append(f"接上之前搁置的任务（判据：{why}；按「{phrase}」理解）")
    # ★ 为什么要把意图也拼进去：用户补的那句（如 `inventory`）**只有服务名**，
    #   规则层看不出"要干什么" → 会再次落到兜底。这一句让意图层读得懂
    #   （规则层认的是「排查/故障」这类人话，不是契约里的 `INCIDENT_RCA`）。
    return f"{original}\n（补充：{message}；{phrase}）", original


async def sync_parked_task(session: Any, *, session_id: str, message: str,
                           tools: Sequence[Mapping[str, Any]] = (),
                           body: dict[str, Any], resumed_from: str = "") -> None:
    """跑完之后维护台账：**又澄清** → 更新"在等什么"；**真的往下走了** → 销账。

    ★ 接续成功时才在响应里写明"这是接上之前那件事"（可追溯：用户看得出我没换话题）。
    ★ 状态落不下去时（共享存储不可用）留痕并进**这一轮的响应**：本轮照常返回，
      而不是把"没记住"变成一次 500（见 `_persist`）。
    """
    if session is None:
        return
    notes = degraded_sink(body)
    snapshots = plans_of_body(body)
    choices = bool(snapshots and not (body.get("explanation") or {}).get("selected_plan_id"))
    if body.get("status") == "clarify" or choices:
        questions = ((body.get("clarification") or {}).get("questions") or [])
        gaps = [str(item.get("slot") or "") for item in questions] or \
            list(body.get("unresolved_entities") or ())
        if choices:
            gaps = list(dict.fromkeys([*gaps, PLAN_GAP]))
        await park(session, session_id,
                   message=resumed_from or message,
                   incident_id=str(body.get("incident_id") or ""),
                   gaps=[gap for gap in gaps if gap],
                   questions=questions,
                   intent=str((body.get("intent") or {}).get("name") or ""),
                   entities=entities_of_body(body), plans=snapshots,
                   tools=tools, degraded=notes)
        return
    # ★ 实测的教训：**只有"接续成功"才销账** ——
    #   用户改问别的事时，新任务跑完就把旧台账清掉的话，那件事就**永远丢了**。
    if resumed_from:
        await clear_parked(session, session_id, message=resumed_from, degraded=notes)
        body["resumed_task"] = {
            "from": resumed_from[:80],
            "note": "本次是把之前搁置的任务接上（同一条管线重跑，判据见 degraded）"}
    await remember_finished(session, session_id, message=message, body=body,
                            tools=tools, degraded=notes)


#: 哪些"结束态"算**做完了事**（值得进近记忆，供紧接着的追问接上）
#: ★ 契约修订 21 起含 `answered`：查数值**给了结果**也是一件做完的事 ——
#:   不进近记忆的话，紧接着的追问（"那连接池呢"）就看不到"上一轮查过什么、算了什么"。
_FINISHED_STATUSES: Final[tuple[str, ...]] = (
    ChatStatus.ANSWERED.value, ChatStatus.ABSTAINED.value,
    ChatStatus.CONCLUDED.value, ChatStatus.REJECTED.value)

#: 这两类意图没做事（拒答 / 知识问答降级），不该进近记忆
_NO_TASK_INTENTS: Final[tuple[str, ...]] = ("OUT_OF_SCOPE", "KNOWLEDGE_QA")


async def remember_finished(session: Any, session_id: str, *, message: str,
                            body: Mapping[str, Any],
                            tools: Sequence[Mapping[str, Any]] = (),
                            degraded: Optional[list[str]] = None) -> None:
    """把**这一轮做完的事**记进近记忆。

    ★ 为什么要有它：`remember_recent` 原先只在**销账**时被调，
      而"问完就结束"的任务（没走澄清 → 从没进过台账）在下一句追问时**什么都不剩**
      → 「那还有别的可能吗」只能被拒答（实测）。这里按"有实质排查就记一条"补齐。

    ★ 同时写**状态与结果摘要**（`summarize_task`）—— 摘要的红线见那条函数。
    """
    intent = str((body.get("intent") or {}).get("name") or "")
    if str(body.get("status") or "") not in _FINISHED_STATUSES or intent in _NO_TASK_INTENTS:
        return
    entities = entities_of_body(body)
    if not entities and not str(message or "").strip():
        return
    await remember_recent(session, session_id, item={
        "message": str(message or ""), "incident_id": str(body.get("incident_id") or ""),
        "intent": intent, "entities": list(entities), "gaps": [],
        # ★ 近记忆也要带工具调用（下一句追问时编织块里能看到"上轮已查：…"）
        **({"tools": [{"name": str(e.get("name") or ""),
                       "title": str(e.get("title") or ""),
                       "result": str(e.get("result") or "")[:120],
                       "ok": bool(e.get("ok", True))}
                      for e in list(tools)[:12] if isinstance(e, Mapping)]} if tools else {}),
        "slots_missing": [], "questions": [],
        "task_summary": summarize_task(body)},
        degraded=degraded if degraded is not None else degraded_sink(body))


#: ★ 摘要里**永远不许出现**的词（没有依据就不能说"已经解决了"）
_FORBIDDEN_IN_SUMMARY: Final[tuple[str, ...]] = ("已修复", "已解决", "修复完成", "问题已消失")


def summarize_task(body: Mapping[str, Any]) -> dict[str, Any]:
    """按**当前事实**给一条任务写摘要（状态 / 缺什么 / 结果一行）。

    ★ `result` **只能由事实推出**：有根因才能说「已定位根因（N 条证据）」，
      有证据无根因只能说「N 条证据，未定位根因」，一条都没有就说「未取到证据」，
      在等用户补东西就说「在等：…」。
    ★ **绝不允许**「已修复 / 已解决」这类**没有依据**的话（连执行通道都没有 ——
      说"已修复"就是编）。
    """
    status = str(body.get("status") or "")
    intent = str((body.get("intent") or {}).get("name") or "")
    questions = ((body.get("clarification") or {}).get("questions") or [])
    missing = [str(item.get("slot") or "") for item in questions] or \
        [str(name) for name in (body.get("unresolved_entities") or ())]
    missing = [name for name in dict.fromkeys(missing) if name]
    evidence = list(body.get("evidence") or ())
    root_cause = body.get("root_cause")
    if status == "clarify" or body.get("clarification"):
        state, result = "blocked", f"在等：{'、'.join(missing) or '（未记）'}"
    elif root_cause:
        state, result = "done", f"已定位根因（证据 {len(evidence)} 条）"
    elif evidence:
        state, result = "done", f"{len(evidence)} 条证据，**未**定位根因"
    else:
        state, result = ("done", "未取到证据") if status else ("active", "进行中")
    summary = {"state": state, "intent": intent, "missing": missing, "result": result,
               "evidence_count": len(evidence),
               "incident_id": str(body.get("incident_id") or "")}
    _assert_summary_consistent(summary)
    return summary


def _assert_summary_consistent(summary: Mapping[str, Any]) -> None:
    """★ 摘要与证据**不许冲突**（构造时就拦，而不是等评审发现）。

    ★ 两条硬判据：① 出现「已定位」就必须真有根因（`state=done` 且证据 ≥ 1）；
      ② 任何摘要都不许出现"已修复/已解决"这类**没有依据**的话。
    """
    text = str(summary.get("result") or "")
    if "已定位" in text and int(summary.get("evidence_count") or 0) <= 0:
        raise ValueError(f"任务摘要说了「已定位」却没有任何证据：{text!r}")
    for word in _FORBIDDEN_IN_SUMMARY:
        if word in text:
            raise ValueError(
                f"任务摘要里出现 {word!r} —— **没有执行通道**，"
                f"说「已修复」就是编：{text!r}")


def describe_task_summary(summary: Mapping[str, Any]) -> str:
    """摘要的人话（进统筹提示词与 `degraded`）。"""
    if not summary:
        return ""
    state = {"active": "进行中", "done": "已完成", "blocked": "在等用户补充"}.get(
        str(summary.get("state")), str(summary.get("state")))
    return (f"{state}；{summary.get('result')}"
            f"（证据 {summary.get('evidence_count', 0)} 条"
            + (f"，缺 {'、'.join(summary.get('missing') or [])}" if summary.get("missing") else "")
            + "）")


def _mentions_service(text: str, service_names: Sequence[str]) -> bool:
    from ..observed.references import service_reference
    return bool(service_reference(text,service_names))


def entities_of_body(body: Mapping[str, Any]) -> tuple[str, ...]:
    """从**这一轮的响应体**里取已解析出的实体（服务 / 集群 / 变更单）。

    ★ 只取**事实字段**（`slots`），不取意图、不取结论 —— 台账记的是"这件事涉及什么"。
    ★ **刻意不收 `env` / `time_window`**：它们有默认值（`prod` / `30m`），
      收进来会让**每条**任务都"含有"这两个词，于是"这句点到了任务实体"的判据
      会被 `prod`、`30m` 这种词频繁误触发 —— 而误触发会削弱**越界拒答**。
    ★ `slots.service` 在契约里是**对象**（`{kind,name,cluster}`），旧形状可能是字符串，
      两种都认（落地时踩过"两种形状只认一种"）。
    """
    slots = body.get("slots")
    if not isinstance(slots, Mapping):
        return ()
    found: list[str] = []
    service = slots.get("service")
    if isinstance(service, Mapping) and service.get("name"):
        found.append(str(service["name"]))
    elif isinstance(service, str) and service:
        found.append(service)
    for name in (slots.get("service_names") or ()):
        if isinstance(name, str) and name:
            found.append(name)
    if str(slots.get("cluster") or "").strip():
        found.append(str(slots["cluster"]))
    if str(slots.get("change_id") or "").strip():
        found.append(str(slots["change_id"]))
    return tuple(dict.fromkeys(found))


def _question_text(question: Any) -> str:
    if isinstance(question, Mapping):
        return str(question.get("question") or "")
    return str(getattr(question, "question", "") or "")


def plans_of_body(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    """优先保存实际展示的结构化完整方案；显式空清单不回退。

    旧响应只有表格时兼容反解析候选行，跳过处置流程行；下一轮选择不重算方案。
    """
    explanation = body.get("explanation") or {}
    if "remediation_plans" in explanation:
        return [dict(plan) for plan in (explanation["remediation_plans"] or ()) if isinstance(plan, Mapping)]
    answer = (explanation.get("answer") or {})
    rows = answer.get("details") if isinstance(answer, Mapping) else None
    out: list[dict[str, Any]] = []
    for row in (rows or ()):
        if not isinstance(row, Mapping):
            continue
        label = str(row.get("label") or "")
        match = re.match(r"方案\s*(\S+?)\s*·\s*(.+)", label)
        if not match:
            continue
        title = match.group(2)
        level = ""
        level_match = re.search(r"（([^（）]*?)）\s*$", title)
        if level_match:
            title = title[:level_match.start()].strip()
            level = level_match.group(1).strip()
        parts = [part.strip() for part in str(row.get("value") or "").split("｜")]
        out.append({
            "option_id": match.group(1), "title": title, "risk_level": level,
            "action_class": class_of_title(title),
            "requires_approval": "需人工审批" in level,
            "rationale": next((part for part in parts if part.startswith("依据")), ""),
            "expected": next((part for part in parts if part.startswith("预期")), ""),
            "rollback": next((part for part in parts if part.startswith("回滚")), ""),
        })
    return out


async def _state_of(session: Any, session_id: str,
                    degraded: Optional[list[str]] = None) -> dict[str, Any]:
    """读会话状态（**读不到不是崩溃，但必须可见**）。

    ★ 为什么要把失败说出来（收口时的实测）：这个函数以前**静默**返回 `{}` ——
      于是"会话状态读不出来"会退化成"**没有在等的任务 / 没有近记忆**"：
      用户会觉得 AI 突然失忆（重复问一遍、或把追问当越界拒答），
      而 `degraded` 里**一个字都没有**。读写失败可以降级，**但不许静默**。
    """
    try:
        state = await session.get_state(session_id)
    except Exception as exc:  # noqa: BLE001 —— 台账不可用不该让请求失败（如实降级）
        if degraded is not None:
            degraded.append(
                f"会话状态读取失败（{type(exc).__name__}: {exc}）→ 本次按"
                f"『没有搁置任务 / 没有近记忆』处理（**可能会重复问一遍**）")
        return {}
    return dict(state) if isinstance(state, Mapping) else {}
