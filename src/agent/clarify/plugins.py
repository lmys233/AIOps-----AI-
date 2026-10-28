"""内置反问插件：一个场景一个插件；问句来自**模板**（不依赖模型可用）。

★ 清单里只有**真会被触发**的插件：`applies_to` 的 gap 必须有人生产
  （`arbiter.requests_from` 或下游不可用那条路），否则就是一个"永远问不出去"的插件 ——
  它不会让任何行为变好，只会让"注册表里有什么"这件事失真。
"""

from __future__ import annotations

from typing import Any, Sequence

from .contract import AskContext, ClarificationRequest, SlotPatch
from .registry import ClarifyRegistry

__all__ = ["build_default_registry"]

#: 环境选项（与 `slots.yaml` 的键一致；出题时用调用方给的 env_options 覆盖）
DEFAULT_ENVS = ("prod", "staging", "dev")


class _EnvPlugin:
    """缺环境 → 问一句（**env 是"去哪套数据里取证"的事实**，猜错代价最大）。"""

    id = "missing_env"
    applies_to = ("env",)
    priority = 30

    def question(self, request: ClarificationRequest, ctx: AskContext) -> Any:
        from ..intent.classifier import ClarificationQuestion  # noqa: PLC0415

        options = tuple(ctx.env_options or DEFAULT_ENVS)
        text = (f"这次要看哪个环境？现在有 {'、'.join(options)} 可选"
                f"（环境决定后续去查哪一套数据，猜错会查到错的系统里）")
        return ClarificationQuestion(slot="env", question=text, options=options,
                                     plugin_id=self.id)

    def apply(self, answer: Any) -> SlotPatch:
        return SlotPatch({"env": str(answer).strip()})


class _ServicePlugin:
    """缺服务/服务有歧义 → 用**被观测清单**把问题问具体。

    ★★ 如果这是一句**泛化的健康询问**（"最近有没有问题？"），措辞要**启发式** ——
      用户原话："AI 得说『不知道您要查哪个系统的问题，您可以明确一下，
      比如：order、server、gateway 等』"。干巴巴一句"你想查哪个服务？"信息量不够：
      用户往往也不知道该说哪个，得顺带告诉他**怎么说**。
    """

    id = "missing_service"
    applies_to = ("service",)
    priority = 60

    def question(self, request: ClarificationRequest, ctx: AskContext) -> Any:
        from ..intent.classifier import ClarificationQuestion  # noqa: PLC0415
        from .arbiter import looks_like_health_scan  # noqa: PLC0415

        options = tuple(ctx.service_options or request.evidence)
        listed = "、".join(options) if options else "（暂时没发现任何被观测服务）"
        if looks_like_health_scan(ctx.message):
            text = (f"不知道您要查哪个系统的问题 —— 您可以明确一下，比如：{listed}；"
                    f"也可以直接说现象（变慢 / 报错 / 延迟升高 / 错误率上涨），我照着去查。")
        else:
            text = f"你想查哪个服务？（当前观测到：{listed}）"
        return ClarificationQuestion(slot="service", question=text, options=options,
                                     plugin_id=self.id)

    def apply(self, answer: Any) -> SlotPatch:
        return SlotPatch({"service": str(answer).strip()})


class _UnknownIntentPlugin:
    """没听懂但确实与本系统相关 → 直接把两类意图摆出来问（与兜底处置一致）。"""

    id = "unknown_intent"
    applies_to = ("intent",)
    priority = 50

    def question(self, request: ClarificationRequest, ctx: AskContext) -> Any:
        from ..intent.classifier import ClarificationQuestion  # noqa: PLC0415

        return ClarificationQuestion(
            slot="intent",
            question="这句我没判定出你想做什么：是**排查故障**，还是**看一下现在的指标**？",
            options=("排查故障", "看指标"), plugin_id=self.id)

    def apply(self, answer: Any) -> SlotPatch:
        text = str(answer)
        intent = "INCIDENT_RCA" if "排查" in text or "故障" in text else "METRIC_QUERY"
        return SlotPatch({"intent": intent})


class _HealthScanPlugin:
    """★★ **泛化的健康询问**（在问"有没有问题"，却没说是哪个服务）→ 启发式反问。

    ★ 用户指示原话："AI 发现没有目标系统，打算反问用户，但是得说：
      『不知道您要查哪个系统的问题，您可以明确一下，**比如：order、server、gateway 等**』
      —— 需要**启发式**的反问。"

    ★ 启发式体现在两处：① 候选来自**被观测到的服务清单**（`/services` 发现的事实，
      不是写死的）；② 顺带给**现象词**，用户不知道怎么说也能照着答。
    ★ 为什么单独一个 gap（`missing_target`）而不是复用 `service`：复用会让**所有**缺服务的
      问句都用上这段"健康询问"的措辞（例如已经说了现象的"P99 涨了"却没说服务）——
      那种情况该问的是服务，不该顺带教育用户怎么提问。
    """

    id = "health_scan_without_target"
    applies_to = ("missing_target",)
    priority = 65

    def question(self, request: ClarificationRequest, ctx: AskContext) -> Any:
        from ..intent.classifier import ClarificationQuestion  # noqa: PLC0415

        options = tuple(ctx.service_options or request.evidence)
        listed = "、".join(options) if options else "（暂时没发现任何被观测服务）"
        text = (f"不知道您要查哪个系统的问题 —— 您可以明确一下，比如：{listed}；"
                f"也可以直接说现象（变慢 / 报错 / 延迟升高 / 错误率上涨），我照着去查。")
        return ClarificationQuestion(slot="service", question=text, options=options,
                                     plugin_id=self.id)

    def apply(self, answer: Any) -> SlotPatch:
        """★ 同时回填**意图**：问的是"有没有问题"，用户给了服务名 → 意思就是"排查它的故障"。

        ★ 为什么必须一起回填：只填 `service` 的话，resume 重跑时原话里那些"问题/正常"
          并不构成异常词，规则层会**再落回兜底**（实测：答完服务名仍然 `OUT_OF_SCOPE`）——
          用户会觉得"答了也没用"。
        """
        return SlotPatch({"service": str(answer).strip(), "intent": "INCIDENT_RCA"})


class _ToolFailurePlugin:
    """工具调用失败 → 反问 + 给出"我查到的事实"与可执行确认命令。"""

    id = "tool_failure"
    applies_to = ("tool_failure",)
    priority = 80

    def question(self, request: ClarificationRequest, ctx: AskContext) -> Any:
        from ..intent.classifier import ClarificationQuestion  # noqa: PLC0415
        from .manual import advice_for  # noqa: PLC0415

        facts = "；".join(request.evidence) or "（没有拿到更多细节）"
        advice = advice_for(ctx.error_code)
        parts = [f"我调工具失败了 —— {facts}。",
                 "要我换一路证据继续，还是你先按下面这条确认？"]
        if advice is not None:
            title, body = advice
            parts.append(f"\n【运维手册 · {title}】\n{body}")
        return ClarificationQuestion(
            slot="tool_failure",
            question="\n".join(parts),
            options=("换一路证据继续", "我先按手册确认", "放弃这次排查"),
            plugin_id=self.id)

    def apply(self, answer: Any) -> SlotPatch:
        return SlotPatch({"tool_failure": str(answer).strip()})


class _SdkUnavailablePlugin:
    """探针全 404 / 端口被占 / 采集器不可达 → 反问 + **直出运维手册原文**。"""

    id = "sdk_unavailable"
    applies_to = ("sdk_unavailable",)
    priority = 90

    def question(self, request: ClarificationRequest, ctx: AskContext) -> Any:
        from ..common.enums import ErrorCode  # noqa: PLC0415
        from ..intent.classifier import ClarificationQuestion  # noqa: PLC0415
        from .manual import advice_for  # noqa: PLC0415

        facts = "；".join(request.evidence) or "（目标不可达）"
        advice = advice_for(ctx.error_code or ErrorCode.TOOL_UNAVAILABLE.value)
        parts = [f"这套服务看起来**没有接上采集 SDK**（或端口被占）—— {facts}。"]
        if advice is not None:
            title, body = advice
            parts.append(f"\n【运维手册 · {title}】\n{body}")
        else:
            parts.append("\n（手册里没有对应条目 —— 我不会编建议，请人工确认端口与启动方式。）")
        return ClarificationQuestion(
            slot="sdk_unavailable",
            question="\n".join(parts),
            options=("我已确认端口/启动方式，重试", "先不改，给我已收集的证据"),
            plugin_id=self.id)

    def apply(self, answer: Any) -> SlotPatch:
        return SlotPatch({"sdk_unavailable": str(answer).strip()})


def build_default_registry(extra: Sequence[Any] = ()) -> ClarifyRegistry:
    """内置插件清单（**显式列出**，禁目录扫描 —— 与工具层同一口径）。

    ★ 为什么禁扫描：扫描 = 谁往目录里放个文件就能让 Agent 多问一句，
      而"问什么"直接影响用户看到的交互（同工具层的"注册即可见"风险）。
    ★★ 末尾 `validate()` 是**真实装配路径**上的加载期自检：注册只挡"声明"层面的错
      （重复 id / 空 gaps），"出题即炸""问句没选项"必须在这里就拦下，
      而不是等用户提问时才发现"问不出来"。
    """
    registry = ClarifyRegistry()
    for plugin in (_SdkUnavailablePlugin(), _ToolFailurePlugin(),
                   _ServicePlugin(), _HealthScanPlugin(),
                   _UnknownIntentPlugin(), _EnvPlugin(), *extra):
        registry.register(plugin)
    registry.validate()
    return registry
