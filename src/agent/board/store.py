"""`BoardStore` —— 黑板读写。

★ 黑板是 Agent 之间**唯一**的共享状态（**证据的唯一落点**）。

"Agent 之间不得有其他通信通道"是硬约束，不是"尽量别直接调用"的风格建议：

| 如果允许点对点通信 | 后果 |
|---|---|
| Log Agent 直接问 Metrics Agent"你看到啥了" | 两路证据**不再独立**，"互相印证"退化成"一个人说了两遍" |
| Change Agent 直接喊 Topology Agent 要依赖图 | 证据的来源链断掉，结论可追溯无从谈起 |
| 任何 Agent 直接改别人的结论 | 审计日志只剩"最终态"，看不到是谁改的 |

所以本模块的两个职责要分清：

1. **追加证据**（`publish`）：**同 `evidence_id` 幂等** —— 重放/重试不会让证据变两份；
2. **受限读取**（`read_evidence`）：**不是所有角色都能读全量**。
   ★ 取证 Agent 之间**互不可见**（否则又回到"点对点通信"）；只有后置角色
   （RCA / Critic / Report）能读全量 —— 它们的职责本来就是"综合别人的证据"。

★ 另外两件事也必须留在黑板上（不能只写在日志里）：

· **"查了没异常"** → `timeline` 里记一条 `signal_checked`。
  它与"没查成"是两件完全不同的事：前者是"排除某假设的依据"，
  后者是"这条线索缺失"。只写日志的话，RCA 在黑板上看不到这个区别。
· **"没查成"** → `degraded_signals` + 原因。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    Final,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    TypeVar,
    runtime_checkable,
)

from ..common.contracts import Evidence, EvidenceBoard, Hypothesis, Slots
from ..state.backend import decode_json, encode_json

_T = TypeVar("_T")

__all__ = [
    "BoardPermissionError",
    "BoardWriteConflict",
    "PublishResult",
    "BoardStore",
    "InMemoryBoardStore",
    "RedisBoardStore",
    "new_evidence_id",
    "READ_ALL_ROLES",
    "BOARD_TTL_SEC",
    "INCIDENT_KEY_PREFIX",
]

logger = logging.getLogger("agent.board")

#: 能读**全量**证据的角色（后置角色）。取证 Agent **不在其中** —— 它们只看自己那一路。
READ_ALL_ROLES: Final[frozenset[str]] = frozenset(
    {"rca_agent", "critic_agent", "report_agent"})

#: 黑板随工作记忆一起过期（会话级 TTL 24h）
BOARD_TTL_SEC: Final[int] = 24 * 3600
INCIDENT_KEY_PREFIX: Final[str] = "aiops:board:"

#: Redis 文档里**不属于契约对象**的留痕键：`EvidenceBoard` 没有 `degraded`
#: 字段（降级清单是排障留痕，见 `mark_degraded`），而契约基类是 `extra="forbid"`。
#: ★ 交给 `model_validate` 之前必须摘掉。
_NON_CONTRACT_KEYS: Final[frozenset[str]] = frozenset({"degraded"})

_CROCKFORD: Final[str] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_evidence_id(*, now: Optional[float] = None) -> str:
    """生成 ULID（26 字符：48 位毫秒时间戳 + 80 位随机，Crockford base32）。

    ★ 为什么自己写而不是引依赖：契约只要求"是 ULID"（有序、可排序、无冲突），
      而 ULID 的构造就这十几行。**时间前缀在前**意味着"按 id 排序 = 按时间排序" ——
      排障时这一点很好用（证据天然按发生顺序排列）。
    """
    millis = int((now if now is not None else time.time()) * 1000)
    chars: list[str] = []
    for shift in range(45, -1, -5):                 # 48 位 → 10 个字符
        chars.append(_CROCKFORD[(millis >> shift) & 0x1F])
    for _ in range(16):                             # 80 位随机 → 16 个字符
        chars.append(_CROCKFORD[random.getrandbits(5)])
    return "".join(chars)


class BoardPermissionError(Exception):
    """该角色不允许做这个黑板操作（例如取证 Agent 想读别人的证据）。"""


class BoardWriteConflict(RuntimeError):
    """同一 incident 的并发写入太多，乐观重试用尽（见 `RedisBoardStore._mutate`）。

    ★ 如实报冲突而不是"最后写的赢"：黑板丢一条证据/一条降级留痕，事后**无从发现**
      （快照里只会少一条），而报一次冲突至少让上层能重试或降级。
    """


def _watch_error() -> Any:
    """`redis.exceptions.WatchError`（**惰性取**：没装 redis 也要能 import 本模块）。

    ★ 返回的是**异常类**，拿到后直接 `except watch_error:`；取不到时返回空元组
      （`except ()` 合法且永不命中）—— 因为那种情况下 `client` 也不可能来自 redis。
    """
    try:
        from redis.exceptions import WatchError  # noqa: PLC0415

        return WatchError
    except Exception:  # noqa: BLE001  (ImportError 或 redis 版本差异)
        return ()


@dataclass(frozen=True)
class PublishResult:
    """一次 publish 的结果（**幂等**：重复的 evidence_id 会被跳过）。"""

    added: int = 0
    duplicates: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return self.added + len(self.duplicates)

    def to_dict(self) -> dict[str, Any]:
        return {"added": self.added, "duplicates": list(self.duplicates)}


# ======================================================================
#  接口
# ======================================================================
@runtime_checkable
class BoardStore(Protocol):
    """黑板读写（五个基本操作 + 留痕操作）。"""

    async def create(self, incident_id: str, slots: Slots) -> EvidenceBoard: ...
    async def get(self, incident_id: str) -> Optional[EvidenceBoard]: ...
    async def publish(self, incident_id: str, agent_role: str,
                      evidence: Sequence[Evidence]) -> PublishResult: ...
    async def read_evidence(self, incident_id: str, agent_role: str,
                            **filter: Any) -> list[Evidence]: ...
    async def propose_hypotheses(self, incident_id: str, agent_role: str,
                                 hypotheses: Sequence[Hypothesis]) -> int: ...
    async def raise_questions(self, incident_id: str, questions: Sequence[str]) -> int: ...
    async def get_evidence(self, incident_id: str, evidence_id: str) -> Optional[Evidence]: ...
    async def mark_degraded(self, incident_id: str, signal: str, reason: str) -> None: ...
    async def record_signal_checked(self, incident_id: str, agent_role: str,
                                    signal: str, result: str) -> None: ...
    async def set_qps(self, incident_id: str, estimates: Mapping[str, float]) -> None: ...
    async def set_attribution(self, incident_id: str, snapshot: Mapping[str, Any]) -> None: ...
    async def snapshot(self, incident_id: str) -> dict[str, Any]: ...


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ======================================================================
#  内存实现（离线单测；也是"黑板语义"的参考实现）
# ======================================================================
@dataclass
class InMemoryBoardStore:
    """内存黑板。**语义与 Redis 版一致**（幂等、受限读取、降级留痕）。"""

    boards: dict[str, EvidenceBoard] = field(default_factory=dict, repr=False)
    timeline: dict[str, list[dict[str, Any]]] = field(default_factory=dict, repr=False)
    degraded: dict[str, list[dict[str, str]]] = field(default_factory=dict, repr=False)

    async def create(self, incident_id: str, slots: Slots) -> EvidenceBoard:
        board = self.boards.get(incident_id)
        if board is None:
            board = EvidenceBoard(incident_id=incident_id, slots=slots)
            self.boards[incident_id] = board
            self.timeline.setdefault(incident_id, []).append(
                {"kind": "board_created", "at": _now_iso()})
        return board

    async def get(self, incident_id: str) -> Optional[EvidenceBoard]:
        return self.boards.get(incident_id)

    def _require(self, incident_id: str) -> EvidenceBoard:
        board = self.boards.get(incident_id)
        if board is None:
            raise KeyError(f"黑板不存在：{incident_id}（先 create）")
        return board

    async def publish(self, incident_id: str, agent_role: str,
                      evidence: Sequence[Evidence]) -> PublishResult:
        board = self._require(incident_id)
        known = {item.evidence_id for item in board.evidence}
        added, duplicates = 0, []
        for item in evidence:
            if item.evidence_id in known:
                duplicates.append(item.evidence_id)
                continue
            if item.agent_role != agent_role:
                # ★ 不许替别人署名：证据的归属必须与调用方一致（可追溯的前提）
                raise BoardPermissionError(
                    f"{agent_role} 试图发布署名 {item.agent_role!r} 的证据 {item.evidence_id} "
                    f"—— 证据的 agent_role 必须与发布者一致"
                )
            board.evidence.append(item)
            known.add(item.evidence_id)
            added += 1
        self.timeline.setdefault(incident_id, []).append(
            {"kind": "evidence_published", "role": agent_role,
             "added": added, "duplicates": duplicates, "at": _now_iso()})
        return PublishResult(added=added, duplicates=tuple(duplicates))

    async def read_evidence(self, incident_id: str, agent_role: str,
                            **filter: Any) -> list[Evidence]:
        board = self._require(incident_id)
        if agent_role not in READ_ALL_ROLES:
            raise BoardPermissionError(
                f"{agent_role} 不能读取全量证据 —— 取证 Agent 之间**互不可见**"
                f"（证据隔离）；能读全量的角色：{sorted(READ_ALL_ROLES)}"
            )
        items = list(board.evidence)
        for key, value in filter.items():
            items = [item for item in items if getattr(item, key, None) == value]
        return items

    async def propose_hypotheses(self, incident_id: str, agent_role: str,
                                 hypotheses: Sequence[Hypothesis]) -> int:
        if agent_role != "rca_agent":
            raise BoardPermissionError(
                f"只有 rca_agent 能提假设，{agent_role} 不行")
        board = self._require(incident_id)
        known = {item.hypothesis_id for item in board.hypotheses}
        added = 0
        for item in hypotheses:
            if item.hypothesis_id in known:
                continue
            board.hypotheses.append(item)
            known.add(item.hypothesis_id)
            added += 1
        return added

    async def raise_questions(self, incident_id: str, questions: Sequence[str]) -> int:
        board = self._require(incident_id)
        raised = 0
        for question in questions:
            text = str(question).strip()
            if text and text not in board.open_questions:
                board.open_questions.append(text)
                raised += 1
        return raised

    async def get_evidence(self, incident_id: str, evidence_id: str) -> Optional[Evidence]:
        board = self._require(incident_id)
        return next((item for item in board.evidence if item.evidence_id == evidence_id), None)

    async def mark_degraded(self, incident_id: str, signal: str, reason: str) -> None:
        """记"这一路**没查成**"。

        ★ 它**不**写进 `EvidenceBoard` 的契约字段：`degraded_signals` 在契约里属于
          `AgentState`（编排层的状态对象），黑板只负责**留痕**（快照里的
          `degraded` 与 timeline）。把降级硬塞进黑板字段会造出第二份"真相"。
        """
        self._require(incident_id)
        entry = {"signal": signal, "reason": reason, "at": _now_iso()}
        self.degraded.setdefault(incident_id, []).append(entry)
        self.timeline.setdefault(incident_id, []).append({"kind": "degraded", **entry})
        logger.warning("黑板：%s 这一路没查成 —— %s", signal, reason)

    async def record_signal_checked(self, incident_id: str, agent_role: str,
                                    signal: str, result: str) -> None:
        """记"这一路查过了"（含结果：`empty` / `ok`）—— 与"没查成"严格区分。

        ★ 与 `mark_degraded` 一样先 `_require`：以前这里用 `setdefault`
          直接长出一条时间线 —— 写错 `incident_id` 时会**静默**留下一条孤儿记录，
          而排障时"这个 incident 到底存不存在"恰恰是要靠黑板回答的问题。
        """
        self._require(incident_id)
        self.timeline.setdefault(incident_id, []).append({
            "kind": "signal_checked", "role": agent_role, "signal": signal,
            "result": result, "at": _now_iso()})

    async def set_qps(self, incident_id: str, estimates: Mapping[str, float]) -> None:
        board = self._require(incident_id)
        for service, qps in estimates.items():
            board.qps_estimates[str(service)] = float(qps)

    async def set_attribution(self, incident_id: str, snapshot: Mapping[str, Any]) -> None:
        self._require(incident_id).attribution_v2 = dict(snapshot)

    async def snapshot(self, incident_id: str) -> dict[str, Any]:
        board = self._require(incident_id)
        return {
            "incident_id": board.incident_id,
            "slots": board.slots.model_dump(mode="json"),
            "evidence": [item.model_dump(mode="json") for item in board.evidence],
            "hypotheses": [item.model_dump(mode="json") for item in board.hypotheses],
            "open_questions": list(board.open_questions),
            "qps_estimates": dict(board.qps_estimates),
            "degraded": list(self.degraded.get(incident_id, [])),
            "timeline": list(self.timeline.get(incident_id, [])),
            "round": board.round,
            "cost": dict(board.cost),
            "attribution_v2": dict(board.attribution_v2),
        }


# ======================================================================
#  Redis 实现（真链路；与工作记忆同一套键前缀风格）
# ======================================================================
@dataclass
class RedisBoardStore:
    """把黑板存成 Redis 里的一个 JSON 文档（会话级 TTL 24h）。

    ★ 为什么用"整份 JSON"而不是"每个证据一条 Redis 结构"：
      黑板是**一次排查**的共享状态，读写都是"整取整存"；
      按证据拆键会引入一堆一致性问题（谁负责版本号？并发 publish 怎么办？），
      而收益（当前的数据量：几十条证据）为零。规模上来后再换 Postgres。

    ★★ 并发口径：**所有写路径都走 `WATCH`/`MULTI`**（见 `_update`），
       并在进程内按 incident 再串行化一道。
       "读出来 → 改内存对象 → 存回去"跨越了 `await`，两个并发写入者会互相覆盖
      （丢证据、丢降级留痕、丢提问）；而**内存后端天生没有这个问题** ——
       也就是说这个 bug 在单测里永远测不出来。
       ★ 真 Redis 实测（`.run/probe_board_redis.py`）：修复前的写法并发 8 写只落下 2 条。
       ★ 两道防线各管一段：进程内锁挡住"同一个网关进程里并行的 5 路取证"
         （否则乐观重试要在同一把键上互相踩，重试次数会随并发数线性膨胀）；
         `WATCH` 挡住**跨进程**（网关多副本）——进程内锁对那个场景完全无效。
    """

    client: Any
    ttl_sec: int = BOARD_TTL_SEC
    #: 乐观并发的重试上限。★ 不设"无限重试"：宁可如实报冲突，也不要把一次
    #: 黑板写入变成一个可能永不返回的循环（上层会超时、会降级，但至少能看到原因）。
    #: 有了进程内锁之后，跨进程冲突是**偶发**事件，8 次足够；真耗尽就如实报错。
    max_write_retries: int = 8
    #: 进程内按 incident 的写锁。★ 一个 incident 一个锁，条目数 = 见过的 incident 数
    #: （每次排查一个，百来字节），随会话 TTL 自然失去意义 —— 不做回收也是安全的。
    _locks: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    def _lock(self, incident_id: str) -> Any:
        lock = self._locks.get(incident_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[incident_id] = lock
        return lock

    def _key(self, incident_id: str) -> str:
        return f"{INCIDENT_KEY_PREFIX}{incident_id}"

    @staticmethod
    def _decode(raw: Any) -> dict[str, Any]:
        """解码一份黑板文档。**只认 JSON 对象**：别的形态宁可报错，也不当成黑板读。"""
        doc = decode_json(raw)
        if not isinstance(doc, dict):
            raise ValueError(
                f"黑板文档不是 JSON 对象（{type(doc).__name__}）—— 拒绝按黑板解析")
        return doc

    @staticmethod
    def _encode(doc: Mapping[str, Any]) -> str:
        return encode_json(doc)

    @staticmethod
    def _as_board(doc: Mapping[str, Any]) -> EvidenceBoard:
        """把 Redis 文档转成契约对象（★ 先摘掉**非契约**的留痕键）。

        Redis 版把"降级清单"也存进同一个文档（`_load`/`snapshot` 要能读回它），
        而 `EvidenceBoard` **没有** `degraded` 字段（见 `mark_degraded` 的说明：
        降级是排障留痕，不是黑板契约字段），且契约基类是 `extra="forbid"` ——
        直接 `model_validate(doc)` 会每次都抛 ValidationError。
        ★ 这不是理论问题：本文件原来就是这么写的，于是 **Redis 黑板从来没能建起来过**
          （真 Redis 实测：`create()` 立刻 ValidationError）——单测只走内存后端，测不到。
        """
        return EvidenceBoard.model_validate(
            {k: v for k, v in doc.items() if k not in _NON_CONTRACT_KEYS})

    async def _load(self, incident_id: str) -> dict[str, Any]:
        raw = await self.client.get(self._key(incident_id))
        if raw is None:
            raise KeyError(f"黑板不存在：{incident_id}（先 create）")
        return self._decode(raw)

    async def _save(self, incident_id: str, doc: Mapping[str, Any]) -> None:
        key = self._key(incident_id)
        await self.client.set(key, self._encode(doc))
        await self.client.expire(key, self.ttl_sec)

    async def _update(self, incident_id: str, mutate: Callable[[dict[str, Any]], _T]) -> _T:
        """一次「读—改—写」：`WATCH` → 读 → `mutate(doc)` → `MULTI/EXEC`；冲突整段重来。

        ★ 为什么是"回调"而不是 `async with` 上下文管理器：`@asynccontextmanager` 的
          生成器**只能 yield 一次** —— 在它内部写重试循环会在冲突重来时第二次 yield，
          于是 `__aexit__` 抛 `RuntimeError: generator didn't stop`（实测踩到）。
          所以重试循环留在外层，把"怎么改"交给调用方传进来的函数。

        ★ `mutate` 必须是**同步**函数（就地改 `doc`）：临界区里多一个 `await`，
          "读到的版本"与"写回的版本"之间就又多出一个窗口。

        两种失败如实抛出：黑板不存在 → `KeyError`（**不隐式创建**：写错 incident_id
        必须响）；连续冲突超过 `max_write_retries` → `BoardWriteConflict`。
        """
        key = self._key(incident_id)
        watch_error = _watch_error()
        last: Optional[BaseException] = None
        async with self._lock(incident_id):        # ① 进程内串行
            for _ in range(max(1, self.max_write_retries)):   # ② 跨进程靠 WATCH
                async with self.client.pipeline(transaction=True) as pipe:
                    try:
                        await pipe.watch(key)
                        raw = await pipe.get(key)
                        if raw is None:
                            raise KeyError(f"黑板不存在：{incident_id}（先 create）")
                        doc = self._decode(raw)
                        result = mutate(doc)
                        # ★ 提交前不再 await 任何"可能让别人插进来"的东西：
                        #   multi/set/expire/execute 之间没有读改写窗口
                        pipe.multi()
                        pipe.set(key, self._encode(doc))
                        pipe.expire(key, self.ttl_sec)
                        await pipe.execute()
                        return result
                    except watch_error as exc:      # type: ignore[misc]
                        last = exc                  # 被别人插队 → 整段重来
                    finally:
                        await pipe.reset()
        raise BoardWriteConflict(
            f"黑板 {incident_id} 连续 {self.max_write_retries} 次写入冲突 —— "
            f"同一 incident 的并发写入太多（最后一次：{last!r}）")

    async def create(self, incident_id: str, slots: Slots) -> EvidenceBoard:
        # ★ "不存在才建"也必须走 WATCH：两个进程同时 create 时，
        #   后到的那个会看到别人已经建好，于是**返回已有的那份**（而不是覆盖它）
        key = self._key(incident_id)
        watch_error = _watch_error()
        async with self._lock(incident_id):
            for _ in range(max(1, self.max_write_retries)):
                async with self.client.pipeline(transaction=True) as pipe:
                    try:
                        await pipe.watch(key)
                        raw = await pipe.get(key)
                        if raw is not None:
                            return self._as_board(self._decode(raw))
                        doc = {
                            "incident_id": incident_id,
                            "slots": slots.model_dump(mode="json"),
                            "evidence": [], "hypotheses": [], "open_questions": [],
                            "qps_estimates": {}, "degraded": [], "timeline": [
                                {"kind": "board_created", "at": _now_iso()}],
                            "round": 1, "cost": {},
                        }
                        pipe.multi()
                        pipe.set(key, self._encode(doc))
                        pipe.expire(key, self.ttl_sec)
                        await pipe.execute()
                        return self._as_board(doc)
                    except watch_error:             # type: ignore[misc]
                        continue
                    finally:
                        await pipe.reset()
        raise BoardWriteConflict(f"黑板 {incident_id} 并发创建冲突（重试 "
                                 f"{self.max_write_retries} 次）")

    async def get(self, incident_id: str) -> Optional[EvidenceBoard]:
        try:
            return self._as_board(await self._load(incident_id))
        except KeyError:
            return None

    async def publish(self, incident_id: str, agent_role: str,
                      evidence: Sequence[Evidence]) -> PublishResult:
        def _apply(doc: dict[str, Any]) -> PublishResult:
            known = {item["evidence_id"] for item in doc.get("evidence", [])}
            added, duplicates = 0, []
            for item in evidence:
                if item.evidence_id in known:
                    duplicates.append(item.evidence_id)
                    continue
                if item.agent_role != agent_role:
                    raise BoardPermissionError(
                        f"{agent_role} 试图发布署名 {item.agent_role!r} 的证据 "
                        f"{item.evidence_id}")
                doc.setdefault("evidence", []).append(item.model_dump(mode="json"))
                known.add(item.evidence_id)
                added += 1
            doc.setdefault("timeline", []).append(
                {"kind": "evidence_published", "role": agent_role, "added": added,
                 "duplicates": duplicates, "at": _now_iso()})
            return PublishResult(added=added, duplicates=tuple(duplicates))

        return await self._update(incident_id, _apply)

    async def read_evidence(self, incident_id: str, agent_role: str,
                            **filter: Any) -> list[Evidence]:
        if agent_role not in READ_ALL_ROLES:
            raise BoardPermissionError(
                f"{agent_role} 不能读取全量证据 —— 取证 Agent 之间**互不可见**"
                f"（证据隔离）")
        doc = await self._load(incident_id)
        items = [Evidence.model_validate(item) for item in doc.get("evidence", [])]
        for key, value in filter.items():
            items = [item for item in items if getattr(item, key, None) == value]
        return items

    async def propose_hypotheses(self, incident_id: str, agent_role: str,
                                 hypotheses: Sequence[Hypothesis]) -> int:
        if agent_role != "rca_agent":
            raise BoardPermissionError(f"只有 rca_agent 能提假设")

        def _apply(doc: dict[str, Any]) -> int:
            known = {item["hypothesis_id"] for item in doc.get("hypotheses", [])}
            added = 0
            for item in hypotheses:
                if item.hypothesis_id in known:
                    continue
                doc.setdefault("hypotheses", []).append(item.model_dump(mode="json"))
                known.add(item.hypothesis_id)
                added += 1
            return added

        return await self._update(incident_id, _apply)

    async def raise_questions(self, incident_id: str, questions: Sequence[str]) -> int:
        def _apply(doc: dict[str, Any]) -> int:
            existing = doc.setdefault("open_questions", [])
            raised = 0
            for question in questions:
                text = str(question).strip()
                if text and text not in existing:
                    existing.append(text)
                    raised += 1
            return raised

        return await self._update(incident_id, _apply)

    async def get_evidence(self, incident_id: str, evidence_id: str) -> Optional[Evidence]:
        doc = await self._load(incident_id)
        for item in doc.get("evidence", []):
            if item["evidence_id"] == evidence_id:
                return Evidence.model_validate(item)
        return None

    async def mark_degraded(self, incident_id: str, signal: str, reason: str) -> None:
        entry = {"signal": signal, "reason": reason, "at": _now_iso()}

        def _apply(doc: dict[str, Any]) -> None:
            doc.setdefault("degraded", []).append(entry)
            doc.setdefault("timeline", []).append({"kind": "degraded", **entry})

        await self._update(incident_id, _apply)
        logger.warning("黑板：%s 这一路没查成 —— %s", signal, reason)

    async def record_signal_checked(self, incident_id: str, agent_role: str,
                                    signal: str, result: str) -> None:
        def _apply(doc: dict[str, Any]) -> None:
            doc.setdefault("timeline", []).append({
                "kind": "signal_checked", "role": agent_role, "signal": signal,
                "result": result, "at": _now_iso()})

        await self._update(incident_id, _apply)

    async def set_qps(self, incident_id: str, estimates: Mapping[str, float]) -> None:
        def _apply(doc: dict[str, Any]) -> None:
            qps = doc.setdefault("qps_estimates", {})
            for service, value in estimates.items():
                qps[str(service)] = float(value)

        await self._update(incident_id, _apply)

    async def snapshot(self, incident_id: str) -> dict[str, Any]:
        return await self._load(incident_id)

    async def set_attribution(self, incident_id: str, snapshot: Mapping[str, Any]) -> None:
        await self._update(incident_id, lambda doc: doc.update(attribution_v2=dict(snapshot)))
