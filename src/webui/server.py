"""Web 台的服务端：零构建静态前端 + 只读 BFF。

★ 三条边界：
  ① 前端**只消费已有契约**（问诊 / SSE / 澄清与审批 / 告警 / 事故 + `/health`）；
  ② BFF **只读聚合**（`/webui/api/capabilities`）：意图表 / 工具解锁表 / 错误码表**从 `enums` 现算** ——
     手抄的表迟早与代码漂移（本项目为此栽过多次）；
  ③ webui 是**软依赖**：它不在时 Agent 的 API 照常工作（`install_webui` 失败只记一条 note）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Final, Optional

from agent.common.enums import (ERROR_RETRY_POLICY, TOOL_UNLOCK_MAP, ErrorCode, Intent)

__all__ = ["SESSION_ENDPOINTS", "STATIC_DIR", "build_capabilities", "install_webui"]

logger = logging.getLogger("webui")

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: 能力页「对外端点」的前半段（问诊 / 告警 / 事故 / 导出）。
_AGENT_ENDPOINTS: Final[tuple[dict[str, str], ...]] = (
    {"method": "POST", "path": "/v1/chat", "what": "同步问诊（结构化结论）"},
    {"method": "POST", "path": "/v1/chat/stream", "what": "流式排查（SSE 事件流）"},
    {"method": "POST", "path": "/v1/chat/resume", "what": "澄清回答 / 审批结论"},
    {"method": "POST", "path": "/v1/alerts", "what": "告警接入（202 入队）"},
    {"method": "GET", "path": "/v1/incidents", "what": "事故列表"},
    {"method": "GET", "path": "/v1/incidents/{id}", "what": "事故详情（与问诊结果同结构）"},
    {"method": "POST", "path": "/v1/remediation/export",
     "what": "把结论/证据/方案导出为 Markdown（导出≠执行，不受 allow_write 影响）"},
)

#: 本地会话管理端点（会话 id **由服务端铸**，前端只拿返回值当指针）。
SESSION_ENDPOINTS: Final[tuple[dict[str, str], ...]] = (
    {"method": "GET", "path": "/v1/sessions",
     "what": "本地会话列表（最近活跃在前，带 is_last 标记）"},
    {"method": "POST", "path": "/v1/sessions",
     "what": "新建会话（**会话 id 由服务端铸**，前端只拿返回值当指针）"},
    {"method": "GET", "path": "/v1/sessions/last",
     "what": "上次会话 + 近期记忆（前端启动/刷新时据此恢复对话）"},
    {"method": "GET", "path": "/v1/sessions/{id}",
     "what": "指定会话 + 近期记忆（只召回近层，不全量回灌）"},
    {"method": "PATCH", "path": "/v1/sessions/{id}",
     "what": "重命名会话（只改展示名，不动记忆与事故）"},
    {"method": "DELETE", "path": "/v1/sessions/{id}",
     "what": "删除会话（会话不存在返回 404；**不删事故**）"},
    {"method": "POST", "path": "/v1/sessions/{id}/select",
     "what": "把该会话设为「上次会话」"},
)

#: 表尾一条（前端状态条读它）。
_HEALTH_ENDPOINT: Final[tuple[dict[str, str], ...]] = (
    {"method": "GET", "path": "/health", "what": "健康与能力自述"},
)

#: 能力页的补充说明（会话 id 的**归属**写在页面上，前端不许自己拼）。
CAPABILITY_NOTES: Final[tuple[str, ...]] = (
    "本页所有展示字段都来自上面这些契约端点，前端不新增业务语义。",
    "abstained / clarify / failed 一律如实显示（不美化失败）。",
    "会话 id 只由服务端铸：前端只保存一个「当前会话 id」指针，"
    "刷新时用上次会话接口恢复近期记忆，恢复不到就如实说明原因。",
)


def build_capabilities() -> dict[str, Any]:
    """把"Agent 能做什么"从**代码**里算出来（前端能力抽屉与工具面板用它）。

    ★ 为什么不让前端写死：意图/工具/错误码三张表都改过很多次；
      前端写死 → 演示时"界面上写着 5 个工具、实际 7 个"这类假话。
    ★ 端点表在模块顶部按组维护，这里只做拼装（顺序：Agent 端点 → 会话端点 → `/health`）。
    """
    tools_by_intent = {name: list(tools) for name, tools in TOOL_UNLOCK_MAP.items()}
    all_tools = sorted({tool for tools in tools_by_intent.values() for tool in tools})
    error_codes = [
        {"code": str(code),
         "retryable": bool(ERROR_RETRY_POLICY.get(str(code), {}).get("retryable", False)),
         "disposition": str(ERROR_RETRY_POLICY.get(str(code), {}).get("disposition", "")),
         "user_visible": bool(ERROR_RETRY_POLICY.get(str(code), {}).get("user_visible", True))}
        for code in ErrorCode
    ]
    return {
        "intents": [{"name": intent.value, "unlocked_tools": tools_by_intent.get(intent.value, [])}
                    for intent in Intent],
        "tools": all_tools,
        "error_codes": error_codes,
        "endpoints": [*_AGENT_ENDPOINTS, *SESSION_ENDPOINTS, *_HEALTH_ENDPOINT],
        "notes": list(CAPABILITY_NOTES),
    }


def install_webui(app: Any, *, static_dir: Optional[Path] = None) -> bool:
    """把 `/webui/`（静态资源）与 `/webui/api/capabilities`（只读 BFF）挂到 Agent 应用上。

    Returns 是否装上（**软依赖**：装不上只记警告，API 照常）。
    """
    directory = static_dir or STATIC_DIR
    try:
        from fastapi.staticfiles import StaticFiles  # noqa: PLC0415

        class _NoStoreStatic(StaticFiles):
            """★ 静态资源一律 `Cache-Control: no-store`。

            ★ 为什么必须显式声明：本机演示台**每次都会改前端**，而浏览器对没有
              `Cache-Control` 的响应会做**启发式缓存** —— 实测用户点了刷新，
              卡片还是**旧结构**（「未决实体」那套），而后端已经在返回新数据 →
              "界面行为与代码不一致"是排查时最费时间的一类问题。
            ★ 只影响 `/webui/` 下的几十 KB 静态文件，不影响任何 API 与遥测。
            """

            async def get_response(self, path: str, scope: Any) -> Any:
                response = await super().get_response(path, scope)
                response.headers["Cache-Control"] = "no-store"
                return response

        @app.get("/webui/api/capabilities", include_in_schema=False)
        async def _capabilities() -> dict[str, Any]:
            return build_capabilities()

        app.mount("/webui", _NoStoreStatic(directory=str(directory), html=True), name="webui")
        logger.info("Web 台已挂载：/webui/（静态目录 %s）", directory)
        return True
    except Exception as exc:  # noqa: BLE001 —— 前端不可用不该拖垮 Agent
        logger.warning("Web 台挂载失败（%s: %s）→ 仅影响前端，API 不受影响",
                       type(exc).__name__, exc)
        return False
