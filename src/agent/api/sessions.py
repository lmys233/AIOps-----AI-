"""本地会话管理的 HTTP 端点：列表 / 新建 / 上次会话 / 恢复 / 删除 / 重命名 / 选为上次。

★ 三件事写在这里，别处不再各写一份：

· **路由顺序**：`/v1/sessions/last` 必须先于 `/v1/sessions/{session_id}` 注册（见下文注释）；
· **恢复只召回近期记忆**：正文来自会话记忆（近层原文），状态与事故号来自本地轮次台账；
· **降级要可见**：会话表读得到但记忆读不到时，仍然返回会话记录 + `degraded` 说明，
  既不 500，也不静默把"读不到"当成"这个会话没有对话"。
"""

from __future__ import annotations

import json
from typing import Any, Final, Mapping, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..sessions import ALERT_SESSION_PREFIX, MAX_SESSIONS, TURNS_META_MAX
from ..common.enums import ChatStatus
from .errors import INVALID_PARAMS, NOT_FOUND

__all__ = ["DEFAULT_LIST_LIMIT", "TURNS_LIMIT", "resume_body"]

#: 契约里的六个落定状态字（用来判断"这一格到底是摘要还是状态码"）
CHAT_STATUS_VALUES: Final[frozenset[str]] = frozenset(status.value for status in ChatStatus)

#: 恢复时召回的**近层轮数**上限：远的走索引，不把全量历史塞回前端。
TURNS_LIMIT: Final[int] = TURNS_META_MAX

#: 列表默认条数（前端侧栏一屏够用；再大也没用，会话表本身有条数上限）。
DEFAULT_LIST_LIMIT: Final[int] = 50


def _register_session_routes(app: FastAPI, service: Any) -> None:
    """注册会话管理的全部端点（列表/新建 · 恢复 · 删除/改名/选择，分三组注册）。"""
    _register_session_list_routes(app, service)
    _register_session_resume_routes(app, service)
    _register_session_edit_routes(app, service)


def _register_session_list_routes(app: FastAPI, service: Any) -> None:
    """列表与新建两个端点。"""

    @app.get("/v1/sessions")
    async def _list(limit: int = DEFAULT_LIST_LIMIT) -> dict[str, Any]:
        return _list_body(service, service.sessions.list(limit=_clamp(limit)))

    @app.post("/v1/sessions", status_code=201)
    async def _create(request: Request) -> Any:
        payload, error = await _body(request, "会话契约")
        if error is not None:
            return error
        return _created_body(service, service.sessions.create(
            title=str(payload.get("title") or "")))


def _register_session_resume_routes(app: FastAPI, service: Any) -> None:
    """「上次会话」与「恢复某个会话」两个端点。"""

    # ★★ `/v1/sessions/last` **必须注册在** `/v1/sessions/{session_id}` **之前**。
    #   FastAPI/Starlette 按注册顺序匹配路径：顺序反过来时 `last` 会被 `{session_id}`
    #   吃掉，于是"上次会话"永远 404 —— 而现象看起来像"这个会话不存在"，
    #   排查时很难想到是路由顺序。
    @app.get("/v1/sessions/last")
    async def _last() -> Any:
        record = service.sessions.last()
        if record is None:
            return _missing("", why="还没有任何会话（先新建一个，或直接发一轮对话）")
        return await resume_body(service, record)

    @app.get("/v1/sessions/{session_id}")
    async def _detail(session_id: str, before: Optional[str] = None) -> Any:
        record = service.sessions.get(session_id)
        if record is None:
            return _missing(session_id)
        return await resume_body(service, record,before=before)


def _register_session_edit_routes(app: FastAPI, service: Any) -> None:
    """删除 / 重命名 / 选择三个端点。"""

    @app.delete("/v1/sessions/{session_id}")
    async def _delete(session_id: str) -> Any:
        from ..observed.legacy_history import block_unscoped_edit
        denied=await block_unscoped_edit(service,session_id)
        if denied is not None:return denied
        history_notes=[]
        if getattr(service,'history',None):
            if service.sessions.get(session_id) is None:return _missing(session_id)
            try:history_notes=await service.history.delete(session_id)
            except Exception as exc:
                return JSONResponse(status_code=503,content={'code':'HISTORY_UNAVAILABLE',
                    'message':'历史档案删除失败：'+type(exc).__name__+'；会话未删除'})
        if not service.sessions.delete(session_id):
            return _missing(session_id)
        degraded = history_notes+await _forget_memory(service, session_id)
        body: dict[str, Any] = {"deleted": True, "session_id": session_id}
        if degraded:
            body["degraded"] = degraded
        return body

    @app.patch("/v1/sessions/{session_id}")
    async def _rename(session_id: str, request: Request) -> Any:
        from ..observed.legacy_history import block_unscoped_edit
        denied=await block_unscoped_edit(service,session_id)
        if denied is not None:return denied
        payload, error = await _body(request, "重命名契约")
        if error is not None:
            return error
        title = str(payload.get("title") or "")
        if not title.strip():        # 空标题 → 422（**不覆盖**原有名字，也不静默当成功）
            return JSONResponse(status_code=422, content={
                "code": INVALID_PARAMS,
                "message": "title 不能为空（空标题不覆盖原有名字）"})
        record = service.sessions.rename(session_id, title)
        if record is None:
            return _missing(session_id)
        return {"session": record.to_dict()}

    @app.post("/v1/sessions/{session_id}/select")
    async def _select(session_id: str) -> Any:
        from ..observed.legacy_history import block_unscoped_edit
        denied=await block_unscoped_edit(service,session_id)
        if denied is not None:return denied
        record = service.sessions.select(session_id)
        if record is None:
            return _missing(session_id)
        return {"session": record.to_dict()}


def _created_body(service: Any, record: Any) -> dict[str, Any]:
    """新建的响应体（含**有界淘汰**的留痕）。"""
    body: dict[str, Any] = {"session": record.to_dict()}
    evicted = service.sessions.last_evicted()
    if evicted:
        body["evicted"] = list(evicted)
        body["degraded"] = [_evicted_note(evicted)]
    return body


def _list_body(service: Any, items: Any) -> dict[str, Any]:
    """列表响应：会话 + **过滤/淘汰的如实留痕**（藏起来的东西不许静默）。"""
    body: dict[str, Any] = {"sessions": [item.to_dict() for item in items]}
    notes: list[str] = []
    hidden = int(service.sessions.hidden_alerts() or 0)
    if hidden > 0:
        body["hidden_alert_sessions"] = hidden
        notes.append(f"另有 {hidden} 条**告警触发**的排查未列出（会话 id 以 "
                     f"{ALERT_SESSION_PREFIX} 开头，不是用户对话；按 id 仍可直接恢复）")
    evicted = service.sessions.last_evicted()
    if evicted:
        body["evicted"] = list(evicted)
        notes.append(_evicted_note(evicted))
    if notes:
        body["degraded"] = notes
    return body


def _evicted_note(evicted: tuple[str, ...]) -> str:
    """有界淘汰的一句话说清（淘汰不许只写日志、在响应里看不见）。"""
    return (f"会话表超过上限 {MAX_SESSIONS} 条 → 已淘汰最久未用的 {len(evicted)} 条"
            f"（{'、'.join(evicted[:5])}）")


async def resume_body(service: Any, record: Any, before: Optional[str] = None) -> Any:
    """恢复响应：会话记录 + **近期轮次** + `degraded`（记忆读不到时在这里如实说明）。"""
    if getattr(service,'history',None):
        from .history_pages import page_body,InvalidHistoryCursor
        try:return await page_body(service,record,before)
        except InvalidHistoryCursor as exc:
            return JSONResponse(status_code=422,content={'code':INVALID_PARAMS,'message':str(exc)})
        except Exception as exc:
            return JSONResponse(status_code=503,content={'code':'HISTORY_UNAVAILABLE',
                'message':'历史档案暂不可读：'+type(exc).__name__+'；请重试，未按空历史处理'})
    if before is not None:
        return JSONResponse(status_code=503,content={'code':'HISTORY_UNAVAILABLE','message':'长期历史未配置，不能加载更早正文'})
    turns, degraded = await _recent_turns(service, record.session_id)
    return {"session": record.to_dict(), "turns": turns, "degraded": degraded}


async def _recent_turns(service: Any,
                        session_id: str) -> tuple[list[dict[str, Any]], list[str]]:
    """读该会话的近期轮次；**读不到就说读不到**（不 500，也不假装没有对话）。

    ★★ 两个来源的分工（以及它们**不同步**时怎么办）：
      正文来自**会话记忆**（可能是别的实例/进程处理的，也可能已过 TTL），
      轮次台账来自**本地会话表**（所有实例共用一份）。于是完全可能
      "台账里有 5 轮、记忆里一条正文都没有"—— 这时**绝不能**静默返回空列表
      （前端会显示"这条会话还没有对话记录"，而事实是"正文不在这里"）。
      本函数于是：① 用台账把缺的那几轮补成**台账行**（`source="ledger"`，
      只给状态摘要/状态码/事故号/时间，**没有原话**）；② 在 `degraded` 里说明原因。
    """
    meta = list(service.sessions.turn_meta(session_id, limit=TURNS_LIMIT))
    memory = getattr(getattr(service, "orchestrator", None), "session", None)
    if memory is None:
        rows = await _attach_incidents(service, _ledger_rows(meta))
        return rows, ["会话记忆未装配（本次只有本地会话表）→ 下面这些行"
                      "来自**本地轮次台账**（状态摘要 + 事故号），**没有原话**"]
    try:
        turns = list(await _read_turns(memory, session_id))
    except Exception as exc:  # noqa: BLE001 —— 记忆不可用是**如实降级**，不是这次请求失败
        rows = await _attach_incidents(service, _ledger_rows(meta))
        return rows, [
            f"会话记忆不可读（{type(exc).__name__}: {exc}）→ 只返回本地轮次台账"
            f"（**不是**「这个会话没有对话」）"]
    rows = await _attach_incidents(service, _view(turns[-TURNS_LIMIT:], meta))
    missing = len(meta) - min(len(turns), len(meta))
    if missing > 0:
        rows = _ledger_rows(meta[:missing]) + rows
        return rows, [
            f"★ 有 {missing} 轮的**正文不在会话记忆里**（可能是另一个实例/进程处理的，"
            f"或记忆已按会话 TTL 过期）→ 这几行只来自**本地轮次台账**"
            f"（状态摘要 + 状态 + 事故号，**没有原话**）"]
    return rows, []


async def _read_turns(memory: Any, session_id: str) -> list[Any]:
    """读会话记忆的近层轮次（新接口优先；注入的假记忆只有旧名字时才走 `history`）。"""
    reader = getattr(memory, "turns", None)
    if reader is not None:
        return list(await reader(session_id, limit=TURNS_LIMIT))
    fallback = getattr(memory, "history", None)
    if fallback is None:
        raise RuntimeError("会话记忆没有可用的读取接口（turns/history 都不在）")
    return list(await fallback(session_id))


def _has_summary(value: str, status: str) -> bool:
    """这一格算不算"状态摘要"。

    ★ 判据是**不是任何状态词**（而不只是"不等于本轮的状态"）：台账与记忆可能来自**不同的那次运行**
      （换个实例、换次重跑都会），于是"记忆里的摘要是 `abstained`、台账里的状态是 `clarify`"
      完全可能发生 —— 只比本轮状态的话，界面就会把 `abstained` 当成摘要印给用户看。
    """
    clean = str(value or "").strip()
    if not clean:
        return False
    return clean not in CHAT_STATUS_VALUES and clean != str(status or "")


def _ledger_rows(meta: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """台账行：**只有状态摘要/状态码/事故号/时间，没有原话**（原话只在会话记忆里）。

    ★ `user_message` 留空是**有意的**：这一轮的用户原话我们真的没有 —— 编一句"（原话缺失）"
      之外的任何内容都会让人以为用户说过那句话。前端据此把这类行单独渲染成「台账」行。
    """
    out: list[dict[str, Any]] = []
    for extra in meta:
        status = str(extra.get("status") or "")
        summary = str(extra.get("summary") or "").strip()
        out.append({"user_message": "",
                    "summary": summary if _has_summary(summary, status) else "",
                    "status": status,
                    "incident_id": str(extra.get("incident_id") or ""),
                    "at": str(extra.get("at") or ""),
                    "source": "ledger"})
    return out


def _view(turns: list[Any], meta: list[dict[str, str]]) -> list[dict[str, Any]]:
    """拼出每轮视图：正文/状态摘要/时间来自记忆，状态码与事故号来自台账（**从尾部对齐**）。

    ★ 两个来源都是"按会话追加写"，所以从**尾部**对齐：最近一轮一定对得上最近一条台账；
      台账比记忆短（例如换过库文件）时，更早那些轮次的状态/事故号留空 —— **不猜**。
    ★ `summary` 取的是记忆里那一轮的**状态摘要**（回答落地时留下的摘要，有界）：
      恢复会话要的是"这一轮聊到哪了"，只给一个状态码读者看不出内容 —— 而它本来就在记忆里，
      没理由不返回（前端也不该为了这一句去猜）。
    ★ 摘要优先取**本地轮次台账**里记的那一句（回答的第一句，落地时写明），
      台账没有（例如更早的库文件）才退回会话记忆里的状态摘要；
      两处都没有就留空、交给状态那一格的大白话（**不猜**）。
    ★ 摘要**恰好等于状态码**时也不算摘要（那说明这一轮只留下了状态字）：原样返回会让界面
      印出一个英文枚举名（而状态那一格已经有了它的中文说法），所以这一格留空。
    """
    overlap = min(len(turns), len(meta))
    tail = [dict(item) for item in meta[len(meta) - overlap:]] if overlap else []
    extras: list[dict[str, str]] = [{} for _ in range(len(turns) - overlap)] + tail
    out: list[dict[str, Any]] = []
    for turn, extra in zip(turns, extras):
        status = str(extra.get("status") or "")
        summary = (str(extra.get("summary") or "").strip()
                   or str(getattr(turn, "response_digest", "") or "").strip())
        out.append({"user_message": str(getattr(turn, "user_message", "") or ""),
                    "summary": summary if _has_summary(summary, status) else "",
                    "status": status,
                    "incident_id": str(extra.get("incident_id") or ""),
                    "at": str(getattr(turn, "at", "") or extra.get("at") or ""),
                    # ★★ 答案**原文**（用户实测："回答原文为什么不给看？"）——
                    #   修前近层只存 200 字符摘要，刷新后只能显示那一句。
                    "answer": _answer_view(turn),
                    # ★★ 这一轮调用过的工具（前端刷新后要恢复成同样的工具卡）
                    "tools": _tools_view(turn),
                    # ★★ 这一轮的**执行过程时间线**（工具卡的位置 + 非工具环节）——
                    #   刷新后「推理过程」要能把"AI 是怎么跑的"原样恢复
                    "steps": _steps_view(turn),
                    "source": "memory"})
    return out


def _tools_view(turn: Any) -> list[dict[str, Any]]:
    """把这一轮调过的工具拼成前端能直接渲染的形状（没有就返回空列表，**不编**）。"""
    out: list[dict[str, Any]] = []
    for item in (getattr(turn, "tools", ()) or ()):
        name, title, params, result, ok = (list(item) + [""] * 6)[:5]
        call_id = str(item[5]) if len(item) > 5 else ""
        out.append({"name": str(name), "title": str(title), "params": str(params),
                    "result": str(result), "ok": bool(ok), "phase": "done",
                    "call_id": call_id})
    return out


def _steps_view(turn: Any) -> list[dict[str, Any]]:
    """把这一轮的**执行过程时间线**拼成前端能直接渲染的形状（没有就空列表，**不编**）。

    ★★ 用户实测："你是不是把思考过程也删了？刷新页面后就不见了，我想要刷新后依旧保留
      AIOps 执行的过程。" —— 时间线里的每一行**文案仍由前端 `summarizeEvent` 生成**
      （服务端只交回有界载荷），这样实时与恢复**共用一处渲染**，不会各说各话。
    ★ 工具那一步只给位置（`call_id` + 名字），卡片内容在 `tools` 里。
    """
    out: list[dict[str, Any]] = []
    for event, payload in (getattr(turn, "steps", ()) or ()):
        try:
            data = json.loads(str(payload) or "{}")
        except (TypeError, ValueError):
            continue                       # 坏载荷丢掉那一步（**不猜**它原本是什么）
        if not isinstance(data, dict):
            continue
        out.append({"event": str(event), "data": data})
    return out


def _answer_view(turn: Any) -> dict[str, Any]:
    """把这一轮存下的**答案原文**拼成前端能直接渲染的形状（没有就返回空 dict，**不编**）。"""
    if getattr(turn,'answer_snapshot',None):return dict(turn.answer_snapshot)
    summary = str(getattr(turn, "answer_summary", "") or "").strip()
    rows = [{"label": str(label), "value": str(value)}
            for label, value in (getattr(turn, "answer_rows", ()) or ())]
    if not summary and not rows:
        return {}
    return {"summary": summary, "details": rows,
            "details_kind": str(getattr(turn, "answer_kind", "") or "table"),
            "advisor_status": str(getattr(turn, "answer_advisor_status", "") or "")}


#: 恢复卡片用到的**事故事实**投影上限（条数 / 字符）：刷新要快，也不该把整份留存吐给前端。
_INCIDENT_BOUNDS: Final[dict[str, int]] = {
    "hypotheses": 6, "evidence": 12, "degraded": 12, "unresolved": 8,
    "line": 400, "text": 1200,
}


def _incident_view(body: Any) -> dict[str, Any]:
    """把留存的事故 body 投影成恢复卡片能用的**有界**事实（没有留存 → 空 dict，**不编**）。

    ★★ 字段名与 `/v1/chat` 的响应**同名**，这是刻意的：前端实时卡片读的就是
      `intent` / `slots` / `evidence` / `cost` / `degraded` …，投影同名之后
      "实时"与"刷新恢复"可以共用**一套**渲染代码 —— 各写一套必然漂移，
      而"刷新后卡片样式退化"正是那么来的。
    ★ 截断必须留痕：条数超上限时把**总数**一起给出去（界面写"共 N 条，这里列前 M 条"）。
    """
    if not isinstance(body, Mapping) or not body:
        return {}
    explanation = body.get("explanation")
    explanation = explanation if isinstance(explanation, Mapping) else {}
    answer = explanation.get("answer")
    answer = answer if isinstance(answer, Mapping) else {}
    hypotheses = [item for item in (body.get("hypotheses") or ()) if isinstance(item, Mapping)]
    evidence = [item for item in (body.get("evidence") or ()) if isinstance(item, Mapping)]
    degraded = [str(item) for item in (body.get("degraded") or ())]
    line = _INCIDENT_BOUNDS["line"]
    return {
        "intent": dict(body.get("intent") or {}),
        "slots": dict(body.get("slots") or {}),
        "unresolved_entities": [str(item) for item in (body.get("unresolved_entities") or ())]
                               [:_INCIDENT_BOUNDS["unresolved"]],
        "hypotheses": [{key: item.get(key) for key in ("rank", "statement", "score", "status")}
                       for item in hypotheses[:_INCIDENT_BOUNDS["hypotheses"]]],
        "hypotheses_total": len(hypotheses),
        "evidence": [{key: str(item.get(key) or "")[:line]
                      for key in ("evidence_id", "signal", "direction", "summary")}
                     for item in evidence[:_INCIDENT_BOUNDS["evidence"]]],
        "evidence_total": len(evidence),
        "proposed_action": body.get("proposed_action"),
        "risk": dict(body["risk"]) if isinstance(body.get("risk"), Mapping) else None,
        "rounds": body.get("rounds"),
        "cost": dict(body.get("cost") or {}),
        "degraded": degraded[:_INCIDENT_BOUNDS["degraded"]],
        "degraded_total": len(degraded),
        "termination_reason": str(body.get("termination_reason")
                                  or (body.get("termination") or {}).get("reason") or "")[:line],
        "internal_report": str(explanation.get("internal_report") or "")
                           [:_INCIDENT_BOUNDS["text"]],
        "answer_source": str(answer.get("source") or ""),
        "answer_note": str(answer.get("note") or "")[:line],
    }


async def _attach_incidents(service: Any,
                            rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给每一轮挂上**该轮事故的事实投影**（拿不到就留空 —— 卡片那边会如实说少哪几格）。

    ★ 读不到**不许**影响恢复本身：留存的条数/内容随时可能过期，而"这一轮聊了什么"
      在会话记忆里 —— 两者不同步时，恢复照常进行，只是卡片上那几格如实标"不可恢复"。
    """
    reader = getattr(service, "get_incident", None)
    if reader is None:
        return rows
    for row in rows:
        incident_id = str(row.get("incident_id") or "")
        if not incident_id:
            continue
        try:
            row["incident"] = _incident_view(await reader(incident_id))
        except Exception as exc:  # noqa: BLE001
            row["incident_error"] = type(exc).__name__
    return rows


async def _forget_memory(service: Any, session_id: str) -> list[str]:
    """删会话时一并删近期记忆；删不掉要**如实说**（本地表已删、记忆可能还在）。"""
    memory = getattr(getattr(service, "orchestrator", None), "session", None)
    clearer = getattr(memory, "clear", None)
    if clearer is None:
        return ["会话记忆没有清除接口 → 本地会话已删除，记忆可能仍在"]
    try:
        await clearer(session_id)
    except Exception as exc:  # noqa: BLE001
        return [f"会话记忆未能删除（{type(exc).__name__}: {exc}）→ 本地会话已删除，"
                f"记忆可能仍在（按会话级 TTL 自然过期）"]
    return []


async def _body(request: Request,
                spec_ref: str) -> tuple[dict[str, Any], Optional[JSONResponse]]:
    """读请求体（**空体合法**：新建与重命名都允许只带默认值），复用统一的解析器。"""
    from .app import _json_object  # noqa: PLC0415 —— app 反向依赖本模块的注册函数

    if not await request.body():
        return {}, None
    return await _json_object(request, spec_ref)


def _clamp(limit: int) -> int:
    """列表条数夹在 `1..MAX_SESSIONS` 之间（越界取边界，不报错）。"""
    return max(1, min(int(limit), MAX_SESSIONS))


def _missing(session_id: str, *, why: str = "") -> JSONResponse:
    """404：本地会话表里没有这一条（删除幂等，也是这个响应）。"""
    return JSONResponse(status_code=404, content={
        "code": NOT_FOUND,
        "message": why or f"本地会话表里没有 {session_id!r}（可能已被删除，或从未创建）"})
