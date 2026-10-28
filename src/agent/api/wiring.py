"""装配生产依赖：模型网关 / 记忆 / 编排器 / 路由器 / 端口。

★ 内容原样搬自原 `agent/server.py`（它曾有 1586 行）；
  **公开导入面一字未改**（`agent/server.py` 门面 + 本包 `__init__.py` 再导出）。
"""

from __future__ import annotations

from ..intent.router import SLOW_ROUTE_THRESHOLD, IntentRouter
from ..llm.gateway import LLMGateway
from typing import Any, Optional

from ..llm.openai_compat import OpenAICompatGateway
from ..common import envfile
import os
from .constants import _usage_sink, logger
from .util import _env


def build_gateway(
    *,
    base_url: str,
    model: str,
    api_key: Optional[str] = None,
) -> Optional[OpenAICompatGateway]:
    """按配置建模型网关。**缺配置就返回 `None`**（= 纯规则层，而不是启动失败）。

    ★ 为什么不 fail-fast：要保证"**无模型/无外网也能判**"。
      缺模型时仍可使用规则/已配置示例向量；低分或歧义需明确澄清，不能承诺全部识别。
    """
    base = (base_url or "").strip()
    name = (model or "").strip()
    if not base or not name:
        return None
    from ..llm.call_policy import Policy
    from ..llm.role_options import command_options,attribution_options
    return OpenAICompatGateway(base_url=base, api_key=api_key, model=name,
        usage_sink=_usage_sink,context_tokens=int(_env('AIOPS_LLM_CONTEXT_TOKENS') or '32768'),
        retry_policy=Policy(timeout=float(_env('AIOPS_MODEL_CALL_TIMEOUT_SEC') or '60'),
            max_attempts=int(_env('AIOPS_MODEL_MAX_ATTEMPTS') or '4'),backoff=float(_env('AIOPS_MODEL_RETRY_BACKOFF_SEC') or '.2'),
            max_backoff=float(_env('AIOPS_MODEL_MAX_BACKOFF_SEC') or '2')),
        review_timeout_sec=float(_env('AIOPS_QA_REVIEW_TIMEOUT_SEC') or '45'),
        structured_mode=_env('AIOPS_LLM_STRUCTURED_MODE') or None,
        role_options={'command':command_options(base,os.getenv('AIOPS_COMMAND_MODEL_OPTIONS')),
            **{role:command_options(base,os.getenv(env)) for role,env in (
                ('qa','AIOPS_QA_MODEL_OPTIONS'),('qa_repair','AIOPS_QA_MODEL_OPTIONS'),
                ('qa_review','AIOPS_QA_REVIEW_MODEL_OPTIONS'),('metric_analyst','AIOPS_METRIC_MODEL_OPTIONS'),
                ('task_dialogue','AIOPS_TASK_DIALOGUE_MODEL_OPTIONS'))},
            'attribution_candidates':attribution_options(base,os.getenv('AIOPS_ATTRIBUTION_MODEL_OPTIONS'))})

def default_session_memory() -> Any:
    """造一个进程内会话记忆（`SessionMemory`）：指代消解与"已问缺口"都靠它。

    ★ 为什么默认是**进程内**：本函数服务于"没注入编排层"的最小装配（单测/离线），
      行为必须与生产一致（否则"接线了"只是测试里的一句话）。
      生产由 `build_orchestrator()` 装配 —— Redis 可用时用 Redis 工作记忆。
    """
    return build_session_memory()

def build_memory_service(env: Optional[dict[str, str]] = None) -> Any:
    """按环境装配**记忆服务**：有 `AIOPS_REDIS_URL` 用 Redis 工作记忆，否则进程内。

    ★ 返回的是 `MemoryService` **本身**（不只是它的 `session`）：处置方案要按
      "这类问题以前怎么处置的"召回程序性记忆与情景记忆，那条路要的是长期记忆入口 ——
      只交 `session` 的话，方案生成就永远看不到记忆。
    """
    from ..memory.service import MemoryService
    from ..memory.stores import RedisWorkingMemory

    environ = dict(os.environ if env is None else env)
    url = (environ.get("AIOPS_REDIS_URL") or "").strip()
    if not url:
        return MemoryService()
    try:
        import redis.asyncio as aioredis  # noqa: PLC0415

        client = aioredis.from_url(url, decode_responses=False)
        return MemoryService(working=RedisWorkingMemory(client=client))
    except Exception as exc:  # noqa: BLE001 —— 记忆降级不该让服务起不来
        logger.warning("Redis 工作记忆装配失败（%s: %s）→ 回退进程内记忆",
                       type(exc).__name__, exc)
        return MemoryService()


def build_session_memory(env: Optional[dict[str, str]] = None) -> Any:
    """按环境装配会话记忆（`MemoryService.session`）。"""
    return build_memory_service(env).session

def build_orchestrator(router: IntentRouter, *,
                       env: Optional[dict[str, str]] = None) -> Any:
    """按环境装配编排层：会话记忆 + 取证执行器 + 检查点。

    ★ 三个零件都**可缺**，且缺了会如实降级（不是静默给个空结论）：
    - 缺会话记忆 → 不做指代消解（`notes` 里写明），澄清仍由意图层的判据驱动；
    - 缺取证执行器 → `INCIDENT_RCA` 会**拒答**（"没有证据可评"），而不是编一个根因；
    - 缺检查点 → 用内存实现，进程崩溃会丢进度，日志里有提示。
    """
    from ..orchestration import Orchestrator, build_checkpointer

    environ = dict(os.environ if env is None else env)
    memory = build_memory_service(environ)          # ★ 同一个记忆服务：会话 + 长期两层共入口
    executor = None
    try:
        from ..agents.wiring import build_executor

        executor = build_executor(environ)
    except Exception as exc:  # noqa: BLE001 —— 装配失败 → 如实降级（不假装能取证）
        logger.warning("取证执行器装配失败（%s: %s）→ 本次不取证",
                       type(exc).__name__, exc)
    enabled=environ.get('AIOPS_ATTRIBUTION_V2','false').lower() in ('true','1','yes')
    if enabled and executor is not None:
        from ..attribution.v2.database_tools import register_database_tools
        register_database_tools(executor.registry)
    return Orchestrator(router=router, session=memory.session, memory=memory,
                        attribution_v2_enabled=enabled,
                        executor=executor,
                        checkpointer=build_checkpointer(
                            environ.get("AIOPS_CHECKPOINT_DIR")))

def build_default_router() -> IntentRouter:
    """按环境变量（含 `.env`）装配默认路由器；没有模型配置就是纯规则层。"""
    envfile.load_env_file()
    gateway: Optional[LLMGateway] = build_gateway(
        base_url=_env("AIOPS_LLM_BASE_URL") or "",
        model=_env("AIOPS_LLM_MODEL") or "",
        api_key=_env("AIOPS_LLM_API_KEY") or None,
    )
    from ..intent.management import build_plugin
    router = IntentRouter(
        routing_plugin=build_plugin(),
        gateway=gateway,
        router_model=_env("AIOPS_ROUTER_MODEL") or "",
        main_model=_env("AIOPS_LLM_MODEL") or "",
        # ★ 复核门槛：默认 0.85（准确性优先）；显式给 0 就退回常量默认，不静默变 0
        slow_route_threshold=float(_env("AIOPS_SLOW_ROUTE_THRESHOLD") or 0.0)
        or SLOW_ROUTE_THRESHOLD,
    )
    if gateway is None:
        logger.warning(
            "未配置 AIOPS_LLM_BASE_URL / AIOPS_LLM_MODEL → "
            "意图规则/示例向量仍可判；低分或向量不可用时没有大模型增强，会显式降级"
        )
    return router
