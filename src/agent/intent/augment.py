"""**上下文编织**：识别之前，把"当前任务"织进输入。

★★ 为什么要有这一层（用户 2026-10-01 指示）

> "我们这个项目**不是完全自由的 agent 项目**，那么我们可以乐观地认为，某次用户输入和
> **上一次的对话**和**最近的任务**有所联系……把最近的对话/任务涉及的**实体、摘要等元数据**
> **编织/附加**到输入中，这样对于输入的（意图）识别控制可能会更好，更好分辨是**越界**
> 还是直接进入 llm。因为我觉得**越界拒答还是有必要留下来的**。"

连续三次同类缺陷（`怎么解决？` → 加代词/省略表；`确认` → 加确认词表；下一个说法还会漏）
说明：**词表是穷举，"在流程中说人话"是开放的**。根因不是词表不全，而是
**识别层看不见流程** —— 它在对一句孤立的话做分类。这一层就是把"看不见"变成"看得见"。

★★ 三条边界（越界即抛错）

| 边界 | 含义 | 为什么 |
|---|---|---|
| **不改原话** | 原话**逐字在最前**，编织只做**追加**并显式标注「（当前任务：…）」 | 用户的话是事实；我的理解只是理解 |
| **只读限定** | "读法"只许取自 `READONLY_PHRASES`（`INTENT_PHRASE` 的四个值） | ★ 编织会把词**送进识别层**；若它带写能力动词，就等于**我替用户说了"重启"** |
| **必须可见** | 编织内容与依据进 `degraded`（`describe()`） | 静默改写用户的话 = 不可追溯 |

★★ 另一条硬纪律：**编织只影响"怎么理解这句话"，不产生任何证据**

四条边界之外，还有一条**与证据层**的纪律：编织产生的文本**不得进入任何 `evidence` 条目**
（证据只能来自工具/日志/指标/变更）。它只影响：识别输入、槽位补充、以及
"这次是接哪件事"的留痕。★ 这条由编排层保证：`evidence` 只由取证 Agent 写。

★★ 关于"编织要不要进规则层的正则匹配面"（我一度想把它挡在外面，**实测后改成放行**）

原设想是"编织只进模型提示词、不进正则面"，理由是"我注入的词会命中我自己的规则 =
自己给自己造证据"。**实测否掉了这个担心**：

| 输入 | 编织前 | 编织后 |
|---|---|---|
| `inventory` | `OUT_OF_SCOPE` 0.20，未定案 | `INCIDENT_RCA` **0.50，仍未定案**（弱提示） |
| `确认` / `帮我写首诗` | 未定案 | **不编织**（实体没命中）→ 照旧越界 |

即：编织后规则层只给出一个**低于决策阈值**的弱提示，最终仍由模型在完整上下文里定案 ——
**不是"我替模型签了字"**。而为把它挡在正则面外，需要动 `router.py`（**正好 800 行**，
规模棘轮上限）或拆走 `merge_slots`（基线里的超长函数，拆了就是新增超长函数）——
**代价与收益不成比例**。于是放行，但用上面那四条边界守住（原话在前 / 只读白名单 /
严格实体命中 / 全程留痕）。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Final, Mapping, Optional, Sequence

from .slots import ACTIVE_SLOTS, SlotConfig, extract_slots, normalize_time_window

__all__ = [
    "INTENT_PHRASE",
    "READONLY_PHRASES",
    "WRITE_VERBS",
    "FILLABLE_GAPS",
    "AugmentTask",
    "Weave",
    "build_tasks",
    "entities_of",
    "pick_task",
    "Candidate", "VECTOR_MIN_SCORE", "MAX_CANDIDATES", "rank_candidates", "render_slots",
    "weave",
    "weave_for",
    "describe",
]

#: ★ 意图枚举 → **规则层认得的白话读法**（回填 / 接续 / 编织三处共用**这一份**）。
#:   它曾经住在 `api/tasks.py`；挪到意图层 —— 因为它是**意图层的事实**
#:   （"这个意图用人话说是什么"），而 `api/tasks.py` 与 `api/resume.py` 都只是**用户**。
INTENT_PHRASE: Final[Mapping[str, str]] = {
    "INCIDENT_RCA": "排查这个服务的故障",
    "METRIC_QUERY": "查一下这个服务当前的指标",
    "CHANGE_IMPACT": "评估这次变更的影响面",
    "REMEDIATION": "给出处置预案（不执行）",
}

#: ★★ **只读读法白名单**：编织文本里唯一允许出现的"意图读法"集合。
#:   它是 `INTENT_PHRASE` 的**值域**（不是另抄一份）—— 加意图时自动覆盖，
#:   而"能不能进编织"由下面的 `WRITE_VERBS` 守卫再兜一层。
READONLY_PHRASES: Final[frozenset[str]] = frozenset(INTENT_PHRASE.values())

#: 写能力动词：编织文本里**一个都不许出现**（`不执行` 是唯一的白名单例外）。
#: ★ 中英各一份：中文是规则层实际认的词，英文是防"顺手写个英文版"漏网。
WRITE_VERBS: Final[tuple[str, ...]] = (
    "重启", "回滚", "扩容", "缩容", "摘流", "引流", "下线", "删除", "修改", "写入",
    "下发", "发布", "变更", "执行", "杀掉", "停止",
    "restart", "rollback", "scale", "delete", "apply", "write", "kill", "exec",
)

#: 允许出现在编织文本里的**否定白名单**（先摘掉再查写动词，避免"不执行"被误判）。
_NEGATED_OK: Final[tuple[str, ...]] = ("不执行", "不重启", "不回滚", "不扩容", "不删除")

#: 任务原话在编织文本里的截断长度（够定位，又不把整段旧话灌进提示词）
_TASK_CLIP: Final[int] = 60

#: 一次最多看几条活跃任务（台账上限本来就是 3；这里再夹一道，防调用方乱传）
_MAX_TASKS: Final[int] = 3

#: ★ **严格**的实体形态：带引号的标识符 / snake_case / 表名点号路径。
#:   刻意**不收**普通英文单词 —— 实体命中会把任务挂到这句上，误命中会**削弱越界拒答**
#:   （用户问一件无关的事，只因为提到了一个常见词就被"接续"进旧任务）。
_ENTITY_RE: Final[re.Pattern[str]] = re.compile(
    r"`([^`]+)`|\"([^\"]+)\"|'([^']+)'|\b([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)\b"
)

#: 时间窗写法（用户原话里的 `30m` / `2h`）
_WINDOW_RE: Final[re.Pattern[str]] = re.compile(r"\b\d+\s*[smhd]\b")

#: 显式接续词：用户明说"我在说之前那件事"
RESUME_WORDS: Final[tuple[str, ...]] = (
    "先前", "刚才", "之前", "上次", "那个", "缺失", "补上", "补充", "接着", "继续", "回到")

#: 召回层"降级"的排障出口。★ 下面三处 `except` 都会**改变判定结果**
#:   （借不到新诉求词表 / 向量路不可用 / 向量算不出来），所以一处都不许静默：
#:   日志是这几条路径**唯一**能留下的痕迹（它们的调用方返回纯值，没有留痕通道）。
logger = logging.getLogger("agent.intent.augment")


def _clip(text: Any, limit: int = _TASK_CLIP) -> str:
    raw = str(text or "").strip().replace("\n", " ")
    return raw if len(raw) <= limit else raw[:limit] + "…"


@dataclass(frozen=True)
class AugmentTask:
    """一条**活跃任务**（从会话台账 `parked_task` 读出来的事实，不含任何意图猜测）。

    ★ `entities` 是**确定性抽取**（服务名走观测清单、标识符走 `_ENTITY_RE`），
      它决定"这句话是不是在说这件事" —— 所以只收**严格**形态。
    """

    message: str = ""
    incident_id: str = ""
    gaps: tuple[str, ...] = ()
    slots_missing: tuple[str, ...] = ()
    intent: str = ""
    entities: tuple[str, ...] = field(default_factory=tuple)
    #: ★ 这条任务**什么时候完成的**（近记忆用；活跃任务为 0）
    finished_at: float = 0.0
    #: ★★ 关联任务（`(incident_id, 共享的实体)`）—— 边上写明"共享了什么"
    related: tuple[tuple[str, str], ...] = ()
    #: ★★ 状态与结果摘要（`blocked`/`done` + 一行结果）
    task_state: str = ""
    #: ★★ 这条任务**实际调过哪些工具**（形如 `promql_read(qps)`）—— 编织块里给模型看，
    #:   让它知道"上一轮已经查过什么"，不重复查（用户 2026-10-03 指示）。
    tools: tuple[str, ...] = ()
    task_result: str = ""

    @property
    def task_id(self) -> str:
        """任务的稳定标识（台账里没有 id 字段 → 用 incident_id，取不到再退回原话）。"""
        return self.incident_id or _clip(self.message, 32)

    def phrases(self) -> tuple[str, ...]:
        """这条任务的只读读法（认不出意图 → 空，调用方给默认）。"""
        phrase = INTENT_PHRASE.get(self.intent, "")
        return (phrase,) if phrase else ()

    def describe(self) -> str:
        known = "、".join(self.entities) or "（未记）"
        import_ = "、".join(self.gaps or self.slots_missing) or "（无）"
        # ★★ 近记忆带**状态与结果摘要** —— 统筹提示词与 `degraded` 都靠这一行
        #   看懂"这件事做到哪一步了"（而不是只有一句原话）。
        state = {"done": "已完成", "blocked": "在等用户补充",
                 "active": "进行中"}.get(self.task_state, "")
        tail = f"；{state}" if state else ""
        if self.task_result:
            tail += f"：{self.task_result}"
        edge = ""
        if self.related:
            edge = "；关联：" + "、".join(f"{shared}" for _id, shared in self.related)
        return f"原话「{_clip(self.message)}」；已知 {known}；在等 {import_}{tail}{edge}"


def entities_of(message: str, *, config: Optional[SlotConfig] = None) -> tuple[str, ...]:
    """从一句话里抽**严格实体**（服务名 / 标识符 / 变更单 / SLO / 集群）。

    ★ 顺序即优先级（服务名在最前，它是运维里最强的实体）；**去重且保序**。
    ★★ **时间窗不算实体**：`30m` / `2h` 这类写法只是观测窗口，几乎每句话都带一个 ——
      把它当实体命中依据，留痕就会写成"点到了任务里的实体 `30m`"（真机现场），
      而那个"实体"跟"这句话在说哪件事"毫无关系（误命中还会削弱越界拒答）。
      所以这里刻意**不再收时间窗**：依据只按服务实体与任务原话写。
    """
    text = str(message or "")
    if not text.strip():
        return ()
    extraction = extract_slots(text, config=config or ACTIVE_SLOTS)
    slots = extraction.slots
    found: list[str] = list(slots.service_names or ())
    for value in (slots.change_id, slots.slo_target, slots.cluster, slots.namespace):
        if value:
            found.append(str(value))
    found.extend(_identifier_tokens(text))
    return tuple(dict.fromkeys(item for item in found if item))


def _identifier_tokens(text: str) -> list[str]:
    """严格的标识符 token（带引号的标识符 / snake_case / 表名点号路径）。

    ★ 只留**真的像标识符**的那一类：时间窗写法（`30m`）与普通英文单词一律不要。
    """
    out: list[str] = []
    for match in _ENTITY_RE.finditer(text):
        token = match.group(0).strip("`\"'")
        if token and not _WINDOW_RE.fullmatch(token):
            out.append(token)
    return out


def build_tasks(parked: Sequence[Mapping[str, Any]], *,
                config: Optional[SlotConfig] = None) -> tuple[AugmentTask, ...]:
    """把会话台账的原始条目转成 `AugmentTask`（**保序**：最近的在前 —— 台账就是这样存的）。

    ★★ 实体取**两处并集**：① 台账里**已记下的**（`entities`，上一轮解析出来的事实）
      ② 从原话里**现抽的**（`entities_of`）。只取其一都不够 ——
      真机实测：台账里只有原话「怎么解决？」时，一个实体都抽不出来，
      于是"接续"重跑时服务又丢了（用户看到的是"你在问哪个服务"，而上一轮其实已经解析出
      `inventory` 了）。
    """
    tasks: list[AugmentTask] = []
    for item in list(parked)[:_MAX_TASKS]:
        if not isinstance(item, Mapping):
            continue
        message = str(item.get("message") or "")
        if not message.strip():
            continue
        recorded = tuple(str(name) for name in (item.get("entities") or ())
                         if str(name).strip())
        tools = tuple(_tool_lines(item.get("tools")))
        tasks.append(AugmentTask(
            message=message,
            tools=tools,
            incident_id=str(item.get("incident_id") or ""),
            gaps=tuple(str(gap) for gap in (item.get("gaps") or ())),
            slots_missing=tuple(str(gap) for gap in (item.get("slots_missing") or ())),
            intent=str(item.get("intent") or ""),
            entities=tuple(dict.fromkeys(
                recorded + entities_of(message, config=config))),
            finished_at=float(item.get("finished_at") or 0.0),
            related=tuple(
                (str(edge.get("incident_id") or ""),
                 "、".join(str(name) for name in (edge.get("shared") or ())))
                for edge in (item.get("related") or ()) if isinstance(edge, Mapping)),
            task_state=str((item.get("task_summary") or {}).get("state") or ""),
            task_result=str((item.get("task_summary") or {}).get("result") or ""),
        ))
    return tuple(tasks)


def _entity_hit(task: AugmentTask, message: str) -> str:
    """任务实体在这句话里**逐字出现** → 返回命中的那个实体（否则空串）。"""
    for entity in task.entities:
        if entity and entity in message:
            return entity
    return ""


def _service_gap_hit(task: AugmentTask, message: str,
                     service_names: Sequence[str]) -> str:
    """这条任务**缺服务**，而这句里点了被观测清单里的服务 → 也算接续。"""
    if "service" not in set(task.slots_missing or task.gaps):
        return ""
    from ..observed.references import service_reference
    return service_reference(message,service_names)


def pick_task(message: str, tasks: Sequence[AugmentTask], *,
              service_names: Sequence[str] = ()) -> tuple[Optional[AugmentTask], str]:
    """这句在说**哪条**活跃任务？→ `(任务或 None, 依据)`

    ★★ 判据顺序（**先实体精确匹配**，向量只兜底）：

      ① **实体命中**：这句话逐字点到了任务里的服务名 / 表名 / 变更单（最强证据）；
      ② **补槽命中**：任务缺服务，而这句给了观测清单里的服务名；
      ③ **显式接续词**：用户明说"之前那个/补上/继续"（语义接续，不一定带实体）。

    ★ 三条都不中 → **不猜**（返回 `None` 与"为什么没命中"）。这是"越界拒答"能守住的前提：
      没有命中就没有编织，识别层看到的还是那句孤立的话。
    """
    text = str(message or "").strip()
    if not text or not tasks:
        return None, "没有活跃任务" if not tasks else "空消息"
    for task in tasks:                                   # ① 实体（最近的在前 → 最近优先）
        hit = _entity_hit(task, text)
        if hit:
            return task, f"这句话点到了任务里的实体 `{hit}`"
    for task in tasks:                                   # ② 补上在等的槽位
        hit = _service_gap_hit(task, text, service_names)
        if hit:
            return task, f"这句补上了任务在等的服务 `{hit}`"
    word = next((item for item in RESUME_WORDS if item in text), "")
    if word:                                             # ③ 显式接续词
        return tasks[0], f"出现显式接续词「{word}」（按最近一条任务理解）"
    return None, "这句话没点到任何活跃任务的实体，也没有接续词"


# ======================================================================
#  ★★ R1 预筛（**先实体，后向量**）
# ======================================================================
#: 向量兜底的相似度阈值：低于它算"没命中"（宁可零候选 → 走既有拒答，也不硬凑一条）
VECTOR_MIN_SCORE: Final[float] = 0.35

#: 交给统筹的候选上限（省 token：小集合才配得上"预筛"这个词）
MAX_CANDIDATES: Final[int] = 3


@dataclass(frozen=True)
class Candidate:
    """一条候选任务 + **依据**（`source` 说明是实体命中还是向量兜底）。"""

    task: AugmentTask
    reason: str = ""
    source: str = "entity"        # entity / service / resume / vector
    score: float = 0.0            # 向量分（实体命中时为 0：**根本不用算向量**）
    state: str = "active"         # active / done（`done` = 刚完成的近记忆）

    def describe(self) -> str:
        tag = "活跃任务" if self.state == "active" else "刚完成"
        return f"[{tag}] {self.task.describe()}（依据：{self.reason}）"


def rank_candidates(message: str, active: Sequence[AugmentTask], *,
                    recent: Sequence[AugmentTask] = (),
                    service_names: Sequence[str] = (),
                    embedder: Optional[Any] = None,
                    min_score: float = VECTOR_MIN_SCORE,
                    limit: int = MAX_CANDIDATES) -> list[Candidate]:
    """★★ R1 预筛：候选 = **活跃 ∪ 近记忆**，按「实体 → 缺参 → 接续词 → 向量」排序。

    ★★ 为什么是这个顺序（用户指示 + 实测）：我们的"任务"是**小集合、实体明确** ——
      结构化匹配又便宜又准；向量只在"用户换了说法、一个实体都没提"时兜底。
      ★ 而且**一旦实体命中就不再算向量**（既省算力，也避免向量把实体结论搅浑）。

    ★ `recent`（刚完成的任务）排在活跃之后：它是"接续的弱证据" ——
      能救回"任务一销账、追问就被拒答"（实测），但**不许**盖过当前活跃任务。

    Returns:
        候选列表（最多 `limit` 条）；**空列表 = 零候选** → 调用方**不触发统筹** →
        越界拒答照旧（这条是拒答正确性的前提）。
    """
    text = str(message or "").strip()
    if not text:
        return []
    active_list, recent_list = list(active), list(recent)
    out = _structured_candidates(text, active_list + recent_list,
                                 active=active_list, service_names=service_names)
    if out:
        # ★★ **结构化命中也要扩一跳** —— 命中的那条任务若有直接关联的
        #   （共享实体）任务，就一起作为候选（"同一个服务的几件事"通常要一起看）。
        return _expand_one_hop(out, active_list + recent_list, limit=limit)
    # ★★ 活跃任务**无条件**作为候选：台账里有它，就说明"我正在等这件事" ——
    #   追问（"那还有别的可能吗"）本来就该接上。这是已经立住的行为，
    #   **不能被 R1 的门槛顺手抹掉**（回归实测抓到过）。
    if active_list:
        return [Candidate(task=task, source="active", state="active",
                          reason="会话里有正在进行的任务（台账未销账）")
                for task in active_list][:limit]
    # ★ 只剩**近记忆**时才需要证据（实体/向量）：刚完成的事是"弱接续"，
    #   没有证据就**不猜** → 零候选 → 越界拒答照旧。
    weak = _recency_candidate(text, recent_list)
    if weak is not None:
        return _expand_one_hop([weak], recent_list, limit=limit)
    return _expand_one_hop(
        _vector_candidates(text, recent_list, embedder=embedder, min_score=min_score,
                           limit=limit, active=active_list),
        recent_list, limit=limit)


def _expand_one_hop(candidates: Sequence[Candidate],
                    all_tasks: Sequence[AugmentTask], *,
                    limit: int) -> list[Candidate]:
    """★★ 召回**扩一跳** —— 命中一条后，把它**直接关联**的任务一起作为候选。

    ★ 只用**边上写着的**关联（`related` = 共享实体），不做多跳、不猜：
      一跳足够覆盖"同一个服务的几件事"，而多跳会把候选集与提示词迅速撑大。
    ★ 关联任务只在**同一批（同一会话、还在近记忆里）**里找 —— 图有界。
    """
    out = list(candidates)
    known = {candidate.task.incident_id for candidate in out if candidate.task.incident_id}
    for candidate in list(out):
        for incident_id, shared in candidate.task.related:
            if not incident_id or incident_id in known:
                continue
            other = next((task for task in all_tasks
                          if task.incident_id == incident_id), None)
            if other is None:
                continue                      # 已过期/不在近记忆里 → 不跨 TTL 召回
            out.append(Candidate(task=other, source="related", state="done",
                                 reason=f"关联任务（共享 `{shared}`）"))
            known.add(incident_id)
            if len(out) >= limit:
                return out
    return out


#: 短追问的判据：**刚做完事**之后，一句很短、不带实体的追问（"那还有别的可能吗"）
#: 就算"弱候选" —— 交给**统筹（LLM）**去判它到底是不是接续。
#:
#: ★ 为什么可以交给 LLM 判而不是代码硬判：区分"那还有别的可能吗"与"帮我写首诗"
#:   靠的是**语义**，词法向量做不到；而统筹的 `out_of_scope` 动作会把越界原样挡回去
#:   （真机已验证：有活跃任务时「帮我写首诗」仍是 `OUT_OF_SCOPE`）。
#: ★ 门开得**很小**：只在"刚刚（`RECENT_FOLLOWUP_SEC` 内）真做完一件事" + "短句"时开。
RECENT_FOLLOWUP_SEC: Final[float] = 300.0
_FOLLOWUP_MAX_CHARS: Final[int] = 14


def _recency_candidate(text: str, recent: Sequence[AugmentTask]) -> Optional[Candidate]:
    """刚做完事 + 一句短追问 → 弱候选（依据写明是"刚完成的任务"，不谎称实体命中）。"""
    if not recent or len(text) > _FOLLOWUP_MAX_CHARS:
        return None
    newest = recent[0]
    finished = float(getattr(newest, "finished_at", 0.0) or 0.0)
    if not finished or (time.time() - finished) > RECENT_FOLLOWUP_SEC:
        return None
    # ★ 借 `api/tasks` 的"新诉求信号词"表（**只有一份**，别抄两处）。
    #   函数内导入：`api.tasks` 在模块级导入本模块，这里只能延迟导入以避免成环。
    try:
        from .continuation import _NEW_ASK_WORDS as new_ask   # noqa: PLC0415
    except Exception as exc:             # noqa: BLE001 —— 借不到就退化成"没有新诉求词"
        logger.warning("借不到新诉求词表（%s: %s）→ 短追问判定退化为"
                       "「没有新诉求词」（可能把新任务当成接续）",
                       type(exc).__name__, exc)
        new_ask = ()
    if any(word in text for word in new_ask):
        return None                       # "为什么/多少/排查…" → 当新任务（不硬接）
    return Candidate(task=newest, source="recency", state="done",
                     reason=(f"刚完成的任务（{time.time() - finished:.0f} 秒前）"
                             f"+ 短追问 → 交给统筹判是不是接续"))


def _structured_candidates(text: str, tasks: Sequence[AugmentTask], *,
                           active: Sequence[AugmentTask],
                           service_names: Sequence[str]) -> list[Candidate]:
    """**结构化匹配**那三段（实体 → 缺参 → 接续词）—— 走完一个任务再下一个。"""
    out: list[Candidate] = []
    for task in tasks:
        state = "active" if task in active else "done"
        hit = _entity_hit(task, text)
        if hit:
            out.append(Candidate(task=task, source="entity", state=state,
                                 reason=f"实体命中 `{hit}`"))
            continue
        filled = _service_gap_hit(task, text, service_names)
        if filled:
            out.append(Candidate(task=task, source="service", state=state,
                                 reason=f"补上了在等的服务 `{filled}`"))
            continue
        if any(word in text for word in RESUME_WORDS):
            out.append(Candidate(task=task, source="resume", state=state,
                                 reason="出现显式接续词"))
    return out


def _vector_candidates(text: str, tasks: Sequence[AugmentTask], *, embedder: Any,
                       min_score: float, limit: int,
                       active: Sequence[AugmentTask]) -> list[Candidate]:
    """**向量兜底**：实体一个都没命中时才走这里（阈值以下一律不算命中）。

    ★ 复用既有的 `memory.embedding.LexicalHashEmbedder`（**词法**近似，不是语义）——
      换真模型只换注入，调用方零改动。
    """
    if embedder is None or not tasks:
        return []
    try:
        from ..memory.embedding import cosine_similarity   # noqa: PLC0415
    except Exception as exc:                # noqa: BLE001 —— 向量路不可用不该影响判定
        logger.warning("向量路不可用（%s: %s）→ 本次召回**没有向量兜底**"
                       "（只剩实体/补槽/接续词三条路）", type(exc).__name__, exc)
        return []
    query_vector = embedder.embed(text)
    scored: list[Candidate] = []
    for task in tasks:
        try:
            score = cosine_similarity(query_vector, embedder.embed(task.message))
        except (ValueError, TypeError) as exc:   # 维度不一致等 → 跳过（不静默当命中）
            logger.warning("向量算不出来（%s: %s）→ 跳过这条候选（%s）",
                           type(exc).__name__, exc, _clip(task.message, 32))
            continue
        if score < min_score:
            continue
        scored.append(Candidate(task=task, source="vector", score=round(score, 4),
                                state="active" if task in list(active) else "done",
                                reason=f"向量兜底（词法相似度 {score:.2f}）"))
    scored.sort(key=lambda item: item.score, reverse=True)
    return scored[:limit]


def _assert_readonly(note: str) -> None:
    """★★ 守卫：编织文本里**不许**出现写能力动词（`不执行` 等否定式除外）。

    ★ 为什么在**构造时**抛错而不是事后过滤：过滤会静默产出一句"被剪过的话"，
      而这里要的是**当场发现**"有人往编织里塞了写动作"——那是个设计错误，不是数据问题。
    """
    scanned = note
    for allowed in _NEGATED_OK:
        scanned = scanned.replace(allowed, "")
    for verb in WRITE_VERBS:
        if verb in scanned:
            raise ValueError(
                f"编织文本里出现了写能力动词 {verb!r} —— 编织只许带**只读**读法"
                f"（编织不得引入任何写能力动词）：{note!r}")


def _tool_lines(raw: Any, *, limit: int = 6) -> list[str]:
    """台账里的工具记录 → 一行行的 `名字(结果摘要)`（**认不出就不写**，不编）。"""
    out: list[str] = []
    for item in (raw or ()):
        entry = item if isinstance(item, Mapping) else {}
        name = str(entry.get("name") or (item if isinstance(item, str) else "")).strip()
        if not name:
            continue
        summary = str(entry.get("result") or entry.get("title") or "").replace("\n", " ")
        out.append(f"{name}（{summary[:40]}）" if summary else name)
        if len(out) >= limit:
            break
    return out


def weave(message: str, task: AugmentTask, *, reason: str,
          phrase: str = "", note_prefix: str = "当前任务") -> "Weave":
    """把任务织进消息：**原话在最前**，追加一段带标注的"当前任务"。

    Args:
        phrase: 只读读法。★ 必须在 `READONLY_PHRASES` 里 —— 否则抛错（fail-closed）。
            传空 → 用任务自己的意图读法；再取不到 → 只用事实，**不编读法**。
    """
    text = str(message or "").strip()
    if not text:
        raise ValueError("编织需要一句非空的用户输入（空消息没有可织的上下文）")
    chosen = phrase or next(iter(task.phrases()), "")
    if chosen and chosen not in READONLY_PHRASES:
        raise ValueError(
            f"读法 {chosen!r} 不在只读白名单 {sorted(READONLY_PHRASES)} 里 "
            f"—— 编织不得自带意图升级")
    parts = [f"{note_prefix}：{_clip(task.message)}",
             f"已知：{'、'.join(task.entities) or '（未记）'}",
             f"在等：{'、'.join(task.gaps or task.slots_missing) or '（无）'}"]
    if getattr(task, "tools", ()):
        # ★★ 让模型看得见"上一轮**实际调过哪些工具**"（用户 2026-10-03 指示）：
        #   不然它会重复查同一批数据，或者不知道手里已经有什么。
        parts.append("上轮已查：" + "、".join(task.tools[:6]))
    if chosen:
        parts.append(f"按「{chosen}」理解")
    note = "（" + "；".join(parts) + "）"
    _assert_readonly(note)
    return Weave(message=text, note=note, matched=task.message, task_id=task.task_id,
                 reason=reason, phrase=chosen, entities=task.entities)


@dataclass(frozen=True)
class Weave:
    """一次编织的产出（**事实 + 留痕**；`woven` 为假时就是"没织"）。"""

    message: str = ""
    note: str = ""
    matched: str = ""
    task_id: str = ""
    reason: str = ""
    phrase: str = ""
    entities: tuple[str, ...] = ()

    @property
    def woven(self) -> bool:
        return bool(self.note and self.matched)

    @property
    def text(self) -> str:
        """真正交给下游的文本（**原话逐字在前**）。"""
        return f"{self.message}\n{self.note}" if self.woven else self.message


def describe(result: Weave) -> str:
    """进 `degraded` 的人话（编织**必须可见**，否则就是静默改写用户的话）。"""
    if not result.woven:
        return ""
    return (f"接上次的任务理解这句（依据：{result.reason or '（未记）'}）——"
            f"{result.note}")


def weave_for(message: str, parked: Sequence[Mapping[str, Any]], *,
              names: Sequence[str] = (),
              config: Optional[SlotConfig] = None,
              recent: Sequence[Mapping[str, Any]] = ()) -> tuple[str, tuple[str, ...]]:
    """**入口的编织动作**（`service._prepare_input` 与单测共用同一个入口）→ `(文本, 留痕)`。

    ★ 三条留痕义务：① 织了什么要写出来；② 守卫拦下也要说（不静默跳过）；
      ③ **没命中不写**（否则每一句都带一条噪声说明，`degraded` 会被淹掉）。
    ★ 没命中就**原样返回** —— "越界拒答"靠这条守住：没有活跃任务的输入，
      识别层看到的仍是那句孤立的话。

    ★★ **两级织法**（强度不同、风险不同）：

    | 依据 | 织什么 | 为什么 |
    |---|---|---|
    | **强**：实体 / 补槽 / 接续词（活跃 ∪ 近记忆） | 完整「当前任务」块（含只读读法） | 这就是"强接续" |
    | **弱**：刚完成 + **像追问的短句** | **只补目标服务**（`（inventory）`），**不注入任何意图词** | 追问通常省略"哪件事"；★ 只补服务名，越界输入（没实体、也不像追问）**一个词都不加** → 拒答不受影响 |
    """
    if _explicit_request(message, config):
        return message, ("本轮意图与服务目标已明确 → 按本轮原话路由；"
                         "历史任务仅作为背景，不扩展查询范围或替用户选择方案",)
    tasks = build_tasks(parked, config=config)
    recents = build_tasks(recent, config=config)
    task, why = pick_task(message, tasks, service_names=names)
    if task is None and recents:
        task, why = pick_task(message, recents, service_names=names)
        if task is not None:
            why = f"（近记忆）{why}"
    if task is None:
        return _service_only_weave(message, recents, names=names)
    try:
        result = weave(message, task, reason=why)
    except ValueError as exc:            # 只读守卫拦下 → 不织，并如实说明（fail-closed）
        return message, (f"上下文编织被只读守卫拦下（{exc}）→ 本次按原话判定",)
    return result.text, (describe(result),)


def _explicit_request(message: str, config: SlotConfig | None) -> bool:
    """完整诉求的目标是当前事实；省略式输入继续沿用上下文编织。"""
    from .rules import match_rules

    extracted = extract_slots(message, config=config or ACTIVE_SLOTS)
    # Matching a goal is enough to keep its original wording; the router still
    # validates confidence. Requiring rule-only certainty lets old RCA text
    # hijack lower-scoring but explicit metric requests before model routing.
    return bool(extracted.slots.service_names and (
        extracted.excluded_services or not match_rules(message,slots_config=config).is_fallback))


def _service_only_weave(message: str, recent: Sequence[AugmentTask], *,
                        names: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    """★ **只补目标服务**（弱证据）。

    ★ 为什么只补服务、不补意图词：追问（"那还有别的可能吗？"）省略的是"哪件事"，
      补上服务就能让下游接着那件事走；而**意图词**一旦注入，越界输入（"帮我写首诗"）
      就可能被自己的规则判成业务意图 —— **拒答正确性**会被削弱。
    ★ 门开得很小：**短句** + **像追问**（结尾问号，或含"还有/别的/其他/那"）+ 刚完成（TTL 内）。
    """
    text = str(message or "").strip()
    if not recent or len(text) > _FOLLOWUP_MAX_CHARS:
        return message, ()
    finished = float(getattr(recent[0], "finished_at", 0.0) or 0.0)
    if not finished or (time.time() - finished) > RECENT_FOLLOWUP_SEC:
        return message, ()
    if not (text.endswith(("？", "?")) or any(word in text for word in _FOLLOWUP_WORDS)):
        return message, ()
    observed = set(names)
    service = next((entity for entity in recent[0].entities
                    if not observed or entity in observed), "")
    if not service:
        return message, ()
    note = (f"按**刚完成的任务**补上目标服务 `{service}`（依据：短追问 + 近记忆；"
            f"**只补服务、不注入意图词** —— 越界输入不会被这条带上业务意图）")
    return f"{text}（{service}）", (note,)


#: "像追问"的信号词（与"新诉求词"互补：有它就更可能是在说同一件事）
_FOLLOWUP_WORDS: Final[tuple[str, ...]] = ("还有", "别的", "其他", "那", "然后", "接着")


#: 统筹**允许**填的槽位（其余一律丢弃）：只有这三个能在"接续"时被补上。
FILLABLE_GAPS: Final[tuple[str, ...]] = ("service", "env", "time_window")


def render_slots(values: Mapping[str, Any], *,
                 config: Optional[SlotConfig] = None) -> tuple[str, tuple[str, ...]]:
    """把统筹给的槽位**按白名单校验后**渲染成一句话 → `(文本, 被丢弃的字段)`。

    ★★ 为什么必须校验：统筹是**模型**，它给的任何值都要过"这是个合法取值吗"，
      否则就是让模型往识别层里塞自由文本（模型输出必须过校验才采信，
      见 `router.py` 头三条约定）。
    ★ 校验口径与契约一致：服务名**必须在观测清单**、环境**必须在允许集合**、
      时间窗**必须能归一**到 `\\d+(s|m|h|d)`；其余字段**一律丢弃**（不留"差不多"的余地）。
    """
    cfg = config or ACTIVE_SLOTS
    accepted: list[str] = []
    rejected: list[str] = []
    values = dict(values or {})
    for name, value in values.items():
        rendered = _valid_slot_value(name, value, cfg) if name in FILLABLE_GAPS else ""
        if rendered:
            accepted.append(f"{name}={rendered}")
        else:
            rejected.append(f"{name}={str(value or '').strip()}")
    if not accepted:
        return "", tuple(rejected)
    note = "（统筹补充：" + "、".join(accepted) + "）"
    _assert_readonly(note)
    return note, tuple(rejected)


def _valid_slot_value(name: str, value: Any, cfg: SlotConfig) -> str:
    """白名单槽位的**取值校验** → 渲染成 `name=value` 的右半段（不合法 → 空串）。

    ★ 判据只有 `FILLABLE_GAPS` 那三个，每个的合法取值来源各不相同：
      服务名看观测清单、环境看数据表的 `envs`、时间窗必须能归一。
    """
    text = str(value or "").strip()
    if name == "service":
        return text if text in set(cfg.service_names) else ""
    if name == "env":
        return text if text in {env for env, _ in cfg.envs} else ""
    if name == "time_window":
        return normalize_time_window(text, config=cfg) or ""
    return ""
