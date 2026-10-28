"""**窄触发的一次 LLM 统筹**。

★★ 它解决的是什么

用户的话在**流程里**说的时候，判据不是"这句话属于哪一类"，而是"**它在说哪件事**"：

| 输入 | 现在（只有意图层） | 有统筹之后 |
|---|---|---|
| `确认` | 再问一遍"你想排查故障还是看指标" | 认出是在接上一件事（★ 本仓库由确定性的规则路径先接住） |
| `那还有别的可能吗` | 越界拒答 | 认出在问上一件事的后续 |
| `帮我写首诗` | 越界拒答 | ★ **仍然拒答**（没有活跃任务 → 统筹根本不被触发） |

★★ 触发是**窄**的（三条同时成立才统筹）

1. **规则层未定案**（`trace.rule.is_fallback`）—— 定案了就不插手；
2. 会话里**有活跃任务** —— 没有旧任务就没得接续（越界拒答靠这条守住）；
3. 当次判定落到 `OUT_OF_SCOPE` —— **只在"本来要拒答"处插手**，正常路由一次都不多花。

★★ 红线：统筹**拿不到执行权**

它只能输出 `{action, task_id, slots, reply_hint, reason}`（只收结构化），
由**代码**映射执行：

| action | 代码做什么 |
|---|---|
| `answer_active_task` | 用 `augment.weave` 把任务织进输入，**重跑一次**意图识别（**最多 1 次**） |
| `new_task` | 什么都不做（照原样走） |
| `out_of_scope` | 什么都不做（越界拒答照旧） |

★ 审批 / L4 禁止 / 写能力双开关**全在代码里**：统筹选什么都不可能执行变更 ——
它连"要不要执行"这个话头都碰不到（`slots` 也只在白名单内被采纳，见 `augment.render_slots`）。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Final, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from ..common.untrusted import fence_block
from ..intent.augment import (
    AugmentTask,
    build_tasks,
    describe as describe_weave,
    render_slots,
    weave,
)
from ..llm.gateway import LLMGateway, LLMError

__all__ = [
    "ACTIONS",
    "MAX_REROUTES",
    "COORD_MAX_TOKENS",
    "CoordinatorDecision",
    "Coordination",
    "LLMCoordinator",
    "coordinator_from_router",
    "takes_memory_block",
    "tasks_from_flags",
    "validate",
]

#: 允许的三种动作（**只有三种** —— 多一个就多一条没人验的路径）
ACTIONS: Final[tuple[str, ...]] = ("answer_active_task", "new_task", "out_of_scope")

#: 一次请求内因统筹而重跑意图识别的上限（防振荡）
MAX_REROUTES: Final[int] = 1

#: 统筹的输出预算（它只做选择与措辞，不写小作文）
COORD_MAX_TOKENS: Final[int] = 512


class CoordinatorDecision(BaseModel):
    """统筹必须按这个结构回答（结构化输出 + 二次校验）。

    ★ 与 `router.ModelIntentDecision` 分开：那个判"这是哪类意图"，
      这个判"**这件事是哪一件、下一步怎么接**"—— 两件事合成一个模型，
             就等于让统筹顺便改意图（那是权限升级）。
    """

    model_config = ConfigDict(extra="forbid")

    action: str = Field(..., description="answer_active_task / new_task / out_of_scope")
    task_id: str = Field(default="", description="接哪条任务（answer_active_task 时必填）")
    slots: dict[str, str] = Field(default_factory=dict,
                                  description="用户这句补上的槽位（只能 service/env/time_window）")
    reply_hint: str = Field(default="", description="一句话说明这句话该怎么理解（给人看）")
    reason: str = Field(default="", description="为什么这么选")


@dataclass(frozen=True)
class Coordination:
    """一次统筹的产出（**结论 + 留痕**；`rerouted` 为假就是"不插手"）。"""

    action: str = "new_task"
    message: str = ""
    note: str = ""
    dropped: str = ""
    task_id: str = ""
    reply_hint: str = ""

    @property
    def rerouted(self) -> bool:
        """要不要**重跑**（只有"接上一件事"才重跑，且必须有可用的消息）。"""
        return self.action == "answer_active_task" and bool(self.message.strip())


class LLMCoordinator:
    """窄触发的统筹器。**没有模型网关就没有它**（规则层独立自洽不变）。"""

    def __init__(self, *, gateway: Optional[LLMGateway] = None, model: str = "",
                 max_reroutes: int = MAX_REROUTES) -> None:
        self._gateway = gateway
        self._model = (model or "").strip()
        self._max_reroutes = max(0, int(max_reroutes))

    @property
    def enabled(self) -> bool:
        return self._gateway is not None and self._max_reroutes > 0

    @property
    def max_reroutes(self) -> int:
        return self._max_reroutes

    # ------------------------------------------------------------------
    async def decide(self, message: str, tasks: Sequence[AugmentTask], *,
                     service_names: Sequence[str] = (),
                     config: Any = None, memory_block: str = "",
                     context_block: str = "") -> Coordination:
        """跑一次统筹。**永远有返回**（失败/非法输出 → `new_task` + 留痕）。

        Args:
            memory_block: ★★ **会话记忆清单**（每段的覆盖窗口 + 抓取时间 + 复用规则）。
                `render_memory_block` 渲染，**空串就是不注入**（没有记忆不留噪声）。
                它只让模型知道"哪些数据已经有了、还新不新" —— **不改判定口径**。
            context_block: ★ **上下文分层小节**（近层原文 / 中层摘要 / 远层索引）。
                同样**空串就是不注入**：没有历史时不留噪声。它只是把"更早的事"
                摆到模型面前（需要旧内容时按索引回查），**不改判定口径**。
        """
        if not self.enabled:
            return Coordination(dropped="未配置模型网关或重跑次数为 0 → 本次不统筹")
        if not tasks:
            return Coordination(dropped="本会话没有活跃任务 → 不统筹（越界拒答照旧）")
        prompt = self._prompt(message, tasks, service_names=service_names,
                              memory_block=memory_block, context_block=context_block)
        try:
            raw = await self._gateway.generate(  # type: ignore[union-attr]
                prompt, CoordinatorDecision, role="coordinator",
                model=self._model or None, temperature=0.0,
                max_tokens=COORD_MAX_TOKENS)
        except LLMError as exc:
            return Coordination(
                dropped=f"统筹模型调用失败（{exc.code}: {exc}）→ 按新任务处理")
        try:
            return validate(raw, tasks, message=message, config=config)
        except ValueError as exc:      # ★ 只读守卫拦下（模型想往输入里塞写动作）
            return Coordination(
                dropped=f"统筹结果被只读守卫拦下（{exc}）→ 按新任务处理")

    # ------------------------------------------------------------------
    def _prompt(self, message: str, tasks: Sequence[AugmentTask], *,
                service_names: Sequence[str], memory_block: str = "",
                context_block: str = "") -> str:
        """拼统筹提示词（**用户原话围栏** —— 它可能含"忽略上面的规则"这类载荷）。"""
        lines = [
            "你是 AIOps 运维助手的**会话统筹**模块：判断**用户这句话在说哪一件正在进行的任务**。",
            "只输出结构化结果。**不要**输出任何执行动作，**不要**编造不存在的任务。",
            "",
            "## 可选 action",
            "- answer_active_task：这句话是在**接续**下面某条任务（补信息/追问进展/要方案/确认）",
            "- new_task：这是一件**新的**运维任务（与下面的任务无关）",
            "- out_of_scope：与运维**无关**（闲聊/写诗/越界/恶意）",
            "",
            "## 正在进行的任务（task_id 只能从这里选）",
            *_task_lines(tasks),
            "",
            f"## 被观测的服务（slots.service 只能从这里选）\n"
            f"{'、'.join(service_names) or '（当前没有观测到任何服务）'}",
            *_memory_section(memory_block),
            *_memory_section(context_block),
            "",
            "## 约定",
            "- slots 只允许填 service / env / time_window（**只填用户这句真的给出的**，没有就留空）",
            "- reply_hint 用一句**人话**说明这句话该怎么理解（不要写内部术语）",
            "",
            *fence_block(message, label="用户输入"),
        ]
        return "\n".join(lines)


def takes_memory_block(coordinator: Any) -> bool:
    """这个统筹器认不认 `memory_block` 形参（注入的替身多半不认 → 退回原调用）。

    ★ 记忆清单是**增强**：送不到不该让统筹失败，也不该改变它的判定口径。
    """
    try:
        params = inspect.signature(coordinator.decide).parameters
    except (TypeError, ValueError):
        return False
    return "memory_block" in params


def _memory_section(block: str) -> list[str]:
    """把会话记忆清单插进提示词（**空串就什么都不插** —— 不留噪声）。"""
    text = (block or "").strip()
    return ["", text] if text else []


def _task_lines(tasks: Sequence[AugmentTask]) -> list[str]:
    return [f"- task_id={task.task_id}：{task.describe()}" for task in tasks]


def _find_task(task_id: str, tasks: Sequence[AugmentTask]) -> Optional[AugmentTask]:
    """按 `task_id` 找任务。

    ★ 只有一个任务且模型没给 id → 认（**无从猜错**）；给错了 id → 不认（fail-closed）。
    """
    wanted = str(task_id or "").strip()
    if not wanted:
        return tasks[0] if len(tasks) == 1 else None
    for task in tasks:
        if task.task_id == wanted or task.message == wanted \
                or task.message.startswith(wanted):
            return task
    return None


def validate(raw: Any, tasks: Sequence[AugmentTask], *, message: str,
             config: Any = None) -> Coordination:
    """★★ **二次校验**：模型说的每一件事都要能对上代码里的事实。

    ★ 丢弃时**必须留痕**（`dropped`）—— 否则"统筹没生效"会被误读成"这句本来就该拒答"。
    """
    action = str(getattr(raw, "action", "") or "").strip().lower()
    if action not in ACTIONS:
        return Coordination(
            dropped=f"统筹给的 action={action!r} 不在 {list(ACTIONS)} 里 → 丢弃（fail-closed）")
    hint = str(getattr(raw, "reply_hint", "") or "").strip()
    if action != "answer_active_task":
        # `new_task` / `out_of_scope` 都**什么都不做**（没有"丢弃"可言，故 dropped 为空）
        return Coordination(action=action, reply_hint=hint)
    task = _find_task(str(getattr(raw, "task_id", "") or ""), tasks)
    if task is None:
        return Coordination(
            action="new_task",
            dropped="统筹给的 task_id 对不上任何活跃任务 → 丢弃（fail-closed，不猜）")
    result = weave(message, task, reason=f"统筹（{str(getattr(raw, 'reason', '') or '未给理由')}）")
    extra, dropped_slots = render_slots(getattr(raw, "slots", None) or {}, config=config)
    text = f"{result.text}\n{extra}" if extra else result.text
    note = describe_weave(result) + (f"；{extra}" if extra else "")
    return Coordination(action=action, message=text, note=note, task_id=task.task_id,
                        reply_hint=hint,
                        dropped=(f"统筹给的槽位被丢弃（不在白名单/取值非法）："
                                 f"{list(dropped_slots)}" if dropped_slots else ""))


def coordinator_from_router(router: Any) -> Optional[LLMCoordinator]:
    """从路由器现建一个统筹器（网关/模型只有一份，见 `api/wiring.build_default_router`）。

    ★ 没配模型 → `None`（**纯规则层照样工作**，与 `IntentRouter` 头两条约定一致）。
    """
    gateway = getattr(router, "gateway", None)
    if gateway is None:
        return None
    return LLMCoordinator(gateway=gateway, model=getattr(router, "main_model", "") or "")


def tasks_from_flags(flags: Mapping[str, Any], *, config: Any = None) -> tuple[AugmentTask, ...]:
    """从 `state.flags["active_tasks"]` 建任务清单（与入口编织用的是**同一份**原始台账）。"""
    raw = flags.get("active_tasks") or ()
    return build_tasks(raw if isinstance(raw, Sequence) else (), config=config)
