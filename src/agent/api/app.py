"""FastAPI 应用：鉴权中间件、异常处理、六个端点。"""

from __future__ import annotations

from typing import Any, Optional
from datetime import datetime, timezone
from ..common.enums import ErrorCode
from fastapi import FastAPI, Request
from ..intent.router import IntentRouter
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.exceptions import RequestValidationError
from ..common import envfile
import hmac
import os
import json
import re
from .constants import (AGENT_SERVICE_NAME, MAX_REQUEST_BYTES, _ENV_OPEN, _ENV_STATE_REDIS,
                        _ENV_TOKEN, _TRUTHY, logger)
from .errors import (DuplicateRequest, INCIDENT_NOT_FOUND, INVALID_PARAMS, UNAUTHORIZED,
                     InvalidAlerts, ResumeError)
from .export import render_markdown, select_plans, utc_stamp
from .requests import ChatRequest
from .service import ChatService
from .sessions import _register_session_routes
from .util import _env, _trace_id_from, _validation_error_body
from .wiring import build_default_router, build_orchestrator


def create_app(*, service: Optional[ChatService] = None,
               router: Optional[IntentRouter] = None,
               auth_token: Optional[str] = None,
               allow_unauthenticated: Optional[bool] = None,
               manage_service: Optional[bool] = None) -> FastAPI:
    """建 Agent 的 Web 应用。`service` / `router` 可注入（离线单测用）。

    `auth_token=None` → 读环境变量 `AIOPS_AGENT_TOKEN`；
    `allow_unauthenticated` 是**显式声明"这次不要鉴权"**（本机开发），
    `None` → 读环境变量 `AIOPS_ALLOW_UNAUTHENTICATED`（`1/true/yes`）。
    注入 service 默认借用，由调用方关闭；manage_service=True 显式转交所有权。
    所有启动/关闭均通过 FastAPI lifespan，部分启动失败也会回收资源。

    ★ **保护要跟着能力走，不能只写在某个启动脚本里**。
      以前"非回环绑定 + 无密钥 → 拒绝启动"只写在 `python -m agent` 的 CLI 里，
      于是 `uvicorn --factory agent.api.app:create_app --host 0.0.0.0` 能绕过它，
      起一个**完全无鉴权**的付费端点（`/v1/alerts` 会按告警条数触发真实排查）。
      现在 fail-closed 判据落在 `create_app`：**没有密钥 + 没有显式声明 → 所有 `/v1/*`
      一律 403**（不是"忘了配密钥就静默裸奔"）。本机开发要放行，必须显式声明
      （CLI 在回环绑定时会替用户声明，见 `__main__.py`）。
    """
    envfile.load_env_file()                      # ★ 与 CLI 的 .env 装载同一入口
    token = auth_token if auth_token is not None else _env(_ENV_TOKEN)
    owned_service = service is None if manage_service is None else manage_service
    owns_router = router is None
    if service is None:
        service = _production_service(router, token)
    elif auth_token is not None:
        # ★ 以前注入 service 时 `auth_token` 被**静默忽略** ——
        #   测试与运维都可能以为配了密钥。显式参数必须**生效**（可预期地覆盖）。
        service.use_auth_token(auth_token)
    from ..common.lifecycle import Resources
    resources = Resources()
    app = FastAPI(
        lifespan=resources.lifespan,
        title="AIOps 故障根因分析 Agent",
        version="0.1.0-p1",
        description="P1/T15 最小服务壳：只做 POST /v1/chat 的同步路径",
    )
    app.state.resources = resources
    app.state.manages_service = owned_service
    app.state.owns_router = owned_service and owns_router
    if owned_service:
        from .resources import service_resources
        service_resources(resources,service,owns_router=owns_router)
    app.state.chat_service = service
    app.state.allow_unauthenticated = _resolve_open_mode(allow_unauthenticated)
    from ..preferences.wiring import install as install_preferences
    install_preferences(app,service,resources)
    # ★ 包体上限必须在**中间件**层，不能只写在 `_json_object` 里。
    #   ★ 顺序：**先装包体闸门、再装鉴权** —— Starlette 里"后加的中间件在最外层"，
    #     所以先加的那个在里面，鉴权仍最先执行（不为未鉴权的请求读 body）。
    _install_body_guard(app)
    _install_auth(app, service, allow_unauthenticated=app.state.allow_unauthenticated)
    _install_error_handlers(app)
    _register_meta_routes(app, service,
                          allow_unauthenticated=app.state.allow_unauthenticated)
    _register_chat_routes(app, service)
    _register_alert_routes(app, service)
    _register_remediation_routes(app, service)
    _register_session_routes(app, service)     # 本地会话管理（不依赖 Redis/PG）
    _register_diagnostic_routes(app,service)
    from ..knowledge.management import install as install_knowledge
    install_knowledge(app,service)
    from ..intent.management import install as install_intents
    install_intents(app, service)
    _mount_webui(app)          # ★ 软依赖地挂 Web 台（细节见函数）
    return app


def _register_diagnostic_routes(app,service):
    from ..diagnostics.wiring import install_routes
    from .evidence_view import install as install_evidence_view
    install_routes(app)
    install_evidence_view(app,service)
    from ..observed.routes import install as install_observed
    from ..observed.relay_worker import install as install_relay
    install_observed(app);install_relay(app)
    service.observed_catalog=app.state.observed_catalog
    from ..observed.workspace_routes import install as install_workspace
    install_workspace(app,service)
    from ..alerts.routes import install as install_warning_routes
    from ..commands.routes import install as install_commands
    install_warning_routes(app,service)
    install_commands(app,service)


def _mount_webui(app: FastAPI) -> None:
    """把 Web 台（`/webui/` + `/webui/api/capabilities`）挂到应用上。

    ★ **软依赖**：装不上只记一条警告 —— 前端不可用不该拖垮 Agent 的 API
      （诊断工具自己不能成为被诊断对象的一部分）。
    """
    try:
        from webui import install_webui  # noqa: PLC0415

        install_webui(app)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Web 台未挂载（%s: %s）→ 仅影响前端", type(exc).__name__, exc)


def _resolve_open_mode(explicit: Optional[bool]) -> bool:
    """这次要不要以"无鉴权"模式跑：显式参数优先，其次环境变量，默认 **False**（fail-closed）。"""
    if explicit is not None:
        return bool(explicit)
    return str(_env(_ENV_OPEN) or "").strip().lower() in _TRUTHY


def _production_service(router: Optional[IntentRouter],
                        auth_token: Optional[str]) -> ChatService:
    """生产装配：编排层拿到**真的**数据源与记忆（单测注入 service 时不走这条路）。

    ★ 共享状态（会话闸门 / 告警账本 / 结果留存 / 待确认动作）
      由 `AIOPS_STATE_REDIS_URL` 决定放哪：
      · 未配置 → 进程内实现（**单进程语义**）；
      · 配置且探测通过 → Redis 实现（多实例 + 重启不丢）；
      · 配置但探测失败 → **退回进程内**并把原因写进 `state.note`（日志 + 响应都要看得到，
        "记不住"这件事不许静默）。
    """
    from ..state import build_state_from_url_sync  # noqa: PLC0415

    the_router = router or build_default_router()
    state = build_state_from_url_sync(_env(_ENV_STATE_REDIS) or "")
    if state.note:
        logger.warning("共享状态降级：%s", state.note)
    from ..history import build_history
    return ChatService(router=the_router,history=build_history(dict(os.environ)),
                       orchestrator=build_orchestrator(the_router),
                       auth_token=auth_token if auth_token is not None
                       else _env(_ENV_TOKEN),
                       state=state)


def _install_body_guard(app: FastAPI) -> None:
    """请求体上限：**四个端点一致**，且 chunked 也挡得住。

    ★ 为什么必须在中间件：`/v1/chat` 与 `/v1/chat/stream` 用 pydantic 模型绑定 body，
      不经过 `_json_object()` —— 于是"解析前拦住"这条不变量只对两个端点成立
      （实跑：同一条超限的 chunked 请求，那两个端点 422 而这两个 413）。
    ★ 为什么在这里读 body：`Content-Length` 是**老实客户端**才给的；
      `Transfer-Encoding: chunked` 没有长度，只能边读边封顶。
    ★ 实测（真 uvicorn + 真 socket，`.run/probe_body_guard_live.py`）：
      · 声明 `Content-Length: 400000` 但只发 100 字节 → **0.00s 就回 413**
        （不必等 body 收完；服务端不会为超大请求把内存/连接先占住）；
      · 分块慢慢发到 600000 字节 → 413，**之后 `/health` 仍然正常**（连接可继续用）；
      · 分块**单个 512 KiB 巨块** → 413（越界量 ≤ 一个分块，见下面的"先判后收"）；
      · 分块**合法小体** → 200（闸门不会把正常请求打断）。
    ★ chunked 的 body 由 Starlette 的 `_CachedRequest` 缓存并**回放**给下游
      （`call_next` 走的是包装过的 receive），所以下游仍能解析出这份 JSON ——
      实测：分块发的 `/v1/chat` 得到 200、`/v1/alerts` 非空数组得到 202。
    """

    @app.middleware("http")
    async def _limit_body(request: Request, call_next: Any) -> Any:
        if request.method in ("POST", "PUT", "PATCH"):
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_REQUEST_BYTES:
                return _too_large(f"声明 {declared}", "请求体上限（中间件）")
            if not (declared and declared.isdigit()):
                body, over = await _read_capped(request)
                if over is not None:
                    return _too_large(over, "请求体上限（中间件，chunked）")
                request._body = body             # 与本中间件的 Request 保持一致
        return await call_next(request)


def _install_auth(app: FastAPI, service: ChatService, *,
                  allow_unauthenticated: bool = False) -> None:
    """鉴权：配了密钥就必须带对；**没配就默认拒服务**。

    三种姿态（按优先级）：
      ① 配了 `AIOPS_AGENT_TOKEN` → 所有 `/v1/*` 必须带对密钥，否则 401；
      ② 没配 + **显式声明**无鉴权（`allow_unauthenticated=True` / `AIOPS_ALLOW_UNAUTHENTICATED=1`）
         → 放行，但 `/health` 会如实标出"当前无鉴权"（本地开发姿态，CLI 只在回环绑定时声明）；
      ③ 没配 + 没声明 → **403 拒服务**（fail-closed：保护跟着能力走，不跟着启动脚本走）。

    `/v1/alerts` 是**设计上要被 Alertmanager 从网络调用**的端点，
    所以"只绑回环"不是安全措施。口径：
      · 未配置 → 不鉴权（本机开发；非回环绑定由 `python -m agent` 拒绝启动）；
      · 配置了 → 所有 `/v1/*` 必须带 `Authorization: Bearer <token>`
        （或 `X-AIOps-Token`），否则 401。
    ★ 用中间件而不是依赖项：中间件在**请求体解析之前**跑 ——
      未鉴权的请求连 body 都不该被解析（少一个攻击面）。
    """

    @app.middleware("http")
    async def _auth(request: Request, call_next: Any) -> Any:
        if request.url.path=='/v1/observed/register':
            from ..observed.routes import registration_auth
            return await registration_auth(app,request,call_next)
        if request.url.path=='/v1/observations/batches':
            return await _diagnostic_auth(app,request,call_next)
        expected = service.auth_token
        if not expected and not allow_unauthenticated \
                and request.url.path.startswith("/v1/"):
            # ★ 未配置密钥且未显式声明 → 拒绝服务（而不是静默裸奔）。
            #   这条判据在 `create_app` 里，所以 `uvicorn --factory …` 也绕不过去。
            return JSONResponse(status_code=403, content={
                "code": UNAUTHORIZED,
                "message": (f"服务未配置共享密钥（{_ENV_TOKEN}）→ 拒绝服务。"
                            f"本机开发请显式声明 {_ENV_OPEN}=1（并确保只绑回环）；"
                            f"生产请配置 {_ENV_TOKEN}")})
        if expected and request.url.path.startswith("/v1/"):
            given = request.headers.get("authorization") or ""
            if given.lower().startswith("bearer "):
                given = given[7:].strip()
            given = given or (request.headers.get("x-aiops-token") or "")
            # Compare encoded bytes, including non-ASCII operator tokens.
            if not hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")):
                # ★ 用 `UNAUTHORIZED`：错误码表里没有"对话入口鉴权"
                #   这一类（与 `INVALID_PARAMS` 同一处境，属于已登记的缺口）。
                return JSONResponse(status_code=401, content={
                    "code": UNAUTHORIZED,
                    "message": f"缺少或错误的共享密钥（请带 Authorization: Bearer "
                               f"<{_ENV_TOKEN}>）"})
        return await call_next(request)


async def _diagnostic_auth(app,request,call_next):
    if request.headers.get('authorization','').startswith('Bearer enr_'):
        from ..observed.ingestion import grant_for_request
        try:request.state.observed_grant=await grant_for_request(app,request)
        except PermissionError:return JSONResponse({'error':'source_not_authorized'},403)
        except Exception:return JSONResponse({'error':'registry_unavailable'},503)
        return await call_next(request)
    from ..diagnostics.wiring import source_auth
    try:source_auth(app,request)
    except PermissionError:return JSONResponse({'error':'source_not_authorized'},403)
    return await call_next(request)


def _install_error_handlers(app: FastAPI) -> None:
    """422 转成结构化错误；重复请求 409；未预期异常记堆栈但**不向前端泄露内部细节**。"""

    @app.exception_handler(RequestValidationError)
    async def _on_invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content=_validation_error_body(exc))

    @app.exception_handler(DuplicateRequest)
    async def _on_duplicate(request: Request, exc: DuplicateRequest) -> JSONResponse:
        """重复请求：**409 + `CONFLICT` + 一句人话**（不回放上一次的响应体）。"""
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(Exception)
    async def _on_internal(request: Request, exc: Exception) -> JSONResponse:
        trace_id = _trace_id_from(request)
        logger.exception("处理请求时发生未预期异常（trace_id=%s）", trace_id)
        return JSONResponse(
            status_code=500,
            content={
                "code": ErrorCode.INTERNAL_ERROR.value,
                "message": "内部错误，请把 trace_id 提供给维护者",
                "trace_id": trace_id,
            },
        )


def _register_meta_routes(app: FastAPI, service: ChatService, *,
                          allow_unauthenticated: bool = False) -> None:
    """`GET /health`（活体 + 模型是否启用 + 降级说明）。"""

    @app.get("/health")
    async def _health() -> dict[str, Any]:
        has_model = getattr(service.router, "enabled", False)
        degraded = ([] if has_model else [
            "未配置模型网关 → 仅规则层（降级已说明）"])
        if not service.auth_token:
            # ★ 无鉴权姿态必须**在健康检查里看得见**（评审/巡检一眼能查）
            degraded.append(
                f"未配置 {_ENV_TOKEN} → 当前为**无鉴权**模式"
                + ("（已显式声明，仅应在回环绑定下使用）"
                   if allow_unauthenticated else "（未显式声明：/v1/* 一律 403）"))
        return {
            "status": "alive",
            "service": AGENT_SERVICE_NAME,
            "stage": "P1/T15（意图识别 + 最小服务壳）",
            "model_enabled": has_model,
            "auth_enabled": bool(service.auth_token),
            "degraded": degraded,
        }


def _register_chat_routes(app: FastAPI, service: ChatService) -> None:
    """同步排查 · 流式排查（SSE）· 恢复中断三个端点。"""

    @app.post("/v1/chat")
    async def _chat(request: Request, payload: ChatRequest) -> dict[str, Any]:
        from ..presentation.transport import project
        return project(await service.handle(payload, trace_id=_trace_id_from(request)),payload.options.response_mode)

    @app.post("/v1/chat/stream")
    async def _chat_stream(request: Request, payload: ChatRequest) -> Any:
        trace_id = _trace_id_from(request)
        # ★★ 重复判定必须在**起流之前**：SSE 的响应头一旦发出去就只能 200 ——
        #   在生成器里判的话，重复请求拿到的是"200 + 流里的 error"，
        #   而契约要的是**409 + CONFLICT**（与 `/v1/chat` 同一判据、同一错误码）。
        try:
            await service.reject_duplicate(payload)
        except DuplicateRequest as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.to_dict())
        return StreamingResponse(
            service.stream_events(payload, trace_id=trace_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "Connection": "keep-alive"})

    @app.post("/v1/chat/resume")
    async def _chat_resume(request: Request) -> JSONResponse:
        trace_id = _trace_id_from(request)
        payload, error = await _json_object(request, "澄清/审批契约")
        if error is not None:
            return error
        try:
            from ..presentation.transport import response_mode
            mode=response_mode(payload)
        except ValueError as exc:
            return JSONResponse(status_code=422,content={'code':INVALID_PARAMS,'message':str(exc)})
        try:
            from ..observed.resume_scope import run
            body = await run(service,payload,trace_id)
        except ResumeError as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.to_dict())
        from ..presentation.transport import project
        return JSONResponse(status_code=200, content=project(body,mode))


def _register_alert_routes(app: FastAPI, service: ChatService) -> None:
    """告警接入（202 不阻塞）· 结果查询与列表两个端点。"""
    from ..alerts.receipts import ReceptionUnavailable

    @app.post("/v1/alerts", status_code=202)
    async def _alerts(request: Request) -> JSONResponse:
        payload, error = await _json_object(request, "告警契约")
        if error is not None:
            return error
        try:
            body = await service.submit_alerts(payload)
        except InvalidAlerts as exc:
            # ★ 只捕**这一种**：`except ValueError` 会把内部任何一次解析抛的
            #   `ValueError` 也变成 422 + 内部原文回客户端（捕获面比需要宽）。
            return JSONResponse(status_code=422,
                                content={"code": INVALID_PARAMS, "message": str(exc)})
        except ReceptionUnavailable as exc:
            return JSONResponse(status_code=503,content={'code':'ALERT_RECEPTION_UNAVAILABLE','message':str(exc)})
        return JSONResponse(status_code=202, content=body)

    @app.get("/v1/incidents/{incident_id}")
    async def _incident(incident_id: str) -> JSONResponse:
        body = await service.get_incident(incident_id)
        if body is None:
            return JSONResponse(status_code=404, content={
                "code": INCIDENT_NOT_FOUND,
                "message": f"没有 {incident_id!r} 这次排查（可能已过期或从未创建）"})
        return JSONResponse(status_code=200, content=body)

    # ---- 列表（评测脚本/排障用；契约未强制，但抑制窗口要靠它复核）----
    @app.get("/v1/incidents")
    async def _incidents() -> dict[str, Any]:
        return {"incidents": await service.list_incidents()}


def _register_remediation_routes(app: FastAPI, service: ChatService) -> None:
    """处置方案导出（Markdown）。

    ★ 它是**导出**，不是写能力：内容由服务端从**已落库**的事实生成（可审计），
      不产生待确认动作、不调 `change_write`、**不读也不受** `options.allow_write` 影响。
    ★ 因此请求体里**没有** `options` —— 调用方想传 `allow_write=true` 也没有字段可传
      （本端点只取需要的三个字段，多传的键一律忽略）。
    """

    @app.post("/v1/remediation/export")
    async def _export(request: Request) -> Response:
        payload, error = await _json_object(request, "导出契约")
        if error is not None:
            return error
        try:
            session_id = _required_text(payload, "session_id")
            incident_id = _required_text(payload, "incident_id")
        except ResumeError as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.to_dict())
        body = await service.get_incident(incident_id)
        if body is None:
            return JSONResponse(status_code=404, content={
                "code": INCIDENT_NOT_FOUND,
                "message": f"没有 {incident_id!r} 这次排查（可能已过期或从未创建）"})
        from ..observed.export_scope import check
        scoped_error=await check(service,payload,body)
        if scoped_error is not None:return scoped_error
        option_id = str(payload.get("option_id") or "").strip()
        plans = select_plans(body, option_id,
                             deployment_form=getattr(service, "deployment_form", ""))
        if plans is None:
            return JSONResponse(status_code=422, content={
                "code": INVALID_PARAMS,
                "message": f"option_id={option_id!r} 不在这次排查的候选方案里"})
        now = datetime.now(timezone.utc)
        markdown = render_markdown(
            session_id=session_id or str(body.get("session_id") or ""),
            incident_id=incident_id, body=body, plans=plans,
            generated_at=str((body.get("explanation") or {}).get("export_generated_at") or ""))
        return Response(
            content=markdown, media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{_safe_name(incident_id, now)}"'})


def _required_text(payload: dict[str, Any], key: str) -> str:
    """取一个**必填**的文本字段（缺了就 422 + 既有错误码，不编默认值）。"""
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ResumeError(422, INVALID_PARAMS, f"{key} 必填（导出契约）")
    return value


def _safe_name(incident_id: str, moment: datetime) -> str:
    """下载文件名：`aiops-<incident_id>-<yyyymmdd-HHMMSS>.md`。

    ★ 事件号要**净化后**再进响应头：它是客户端可控的（导出接口的入参），
      带引号或换行就等于让调用方决定响应头的形状。
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", str(incident_id or ""))[:64] or "incident"
    return f"aiops-{safe}-{utc_stamp(moment)}.md"


async def _read_capped(request: Request) -> tuple[bytes, Optional[str]]:
    """把 body 读进内存，**边读边封顶** —— 全仓**唯一实现**。

    ★ 这段逻辑以前有**两份**，且口径不同 ——
      `_install_body_guard` 的中间件是"**先判后收**"（`body` 永不超过上限），
      而 `_json_object` 是"**先 extend 后判**"（会先超出上限一个分块）。
      同一件事两种写法，改一份漂移一份。现在两边都调它。

    返回 `(body, 超限说明或 None)`；超限时 body 为空（超出的部分不再读）。
    """
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
            return b"", f"实际读到 {len(body) + len(chunk)} 字节"
        body.extend(chunk)
    return bytes(body), None


async def _json_object(request: Request,
                       spec_ref: str) -> tuple[dict[str, Any], Optional[JSONResponse]]:
    """把请求体读成对象；超限 413 / 不是合法 JSON / 不是对象 → 422。"""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_REQUEST_BYTES:
        # ★ **先看声明长度**再读 body（解析之前挡住）。
        return {}, _too_large(f"声明 {declared}", spec_ref)
    # ★ 只看 `Content-Length` 是**老实客户端才成立**的护栏 ——
    #   `Transfer-Encoding: chunked`（没有长度）或非纯数字长度会整段跳过。
    #   → **边读边封顶**，实现只有一份（`_read_capped`）。
    body, over = await _read_capped(request)
    if over is not None:
        return {}, _too_large(over, spec_ref)
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}, JSONResponse(status_code=422, content={
            "code": INVALID_PARAMS, "message": f"请求体不是合法 JSON（{spec_ref}）"})
    if not isinstance(payload, dict):
        return {}, JSONResponse(status_code=422, content={
            "code": INVALID_PARAMS, "message": f"请求体必须是对象（{spec_ref}）"})
    return payload, None


def _too_large(why: str, spec_ref: str) -> JSONResponse:
    return JSONResponse(status_code=413, content={
        "code": INVALID_PARAMS,
        "message": (f"请求体超过上限 {MAX_REQUEST_BYTES} 字节（{why}）—— "
                    f"请分批发送（{spec_ref}）")})
