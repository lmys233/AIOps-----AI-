"""`MemoryService` —— 记忆四层的**唯一入口**。

```python
recall(slots, symptom, top_k=3) -> list[MemoryHit]     # 六步读取（含并行三路召回）
write(incident, board)          -> WriteResult         # 五道闸门（**绝不抛异常**）
feedback(memory_id, useful)     -> None                # 更新 hit_count 与成功率
```

三条实现约束：

1. **`write()` 永不抛异常**：被闸门拦下、存储不可用、影子回归熔断……
   全部走 `WriteResult(skipped=True, reason=...)`。
   记忆写入是**增强**，它失败不该让整次排查崩。
2. **`recall()` 区分"没查成"与"没命中"**：前者进 `degraded_paths`，
   后者是正常的空结果。混起来 = 把"记忆库连不上"读成"没有相似案例"。
3. **只在故障关闭后写长期记忆**：`write()` 的入参是 `incident` + `BoardStore`，
   调用方负责"故障已关闭"这个前提；本层不猜。反过来，
   **排查中途需要落盘的东西走工作记忆**，不碰长期记忆。
"""

from __future__ import annotations
from ..common.lifecycle import close_pool

from .recall import MemoryRecall, RecallResult

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final, Mapping, Optional, Sequence

from ..common.contracts import EvidenceBoard, MemoryHit, Slots, WriteResult
from ..common.enums import VerificationMethod
from .embedding import LEXICAL_EMBEDDER_NAME, Embedder, LexicalHashEmbedder
from .gates import (
    GateReport,
    gate_dedupe,
    gate_redaction,
    gate_root_cause,
    gate_shadow_regression,
    gate_verification,
)
from .session import SessionMemory
from .stores import (
    PROCEDURE_ID_PREFIX,
    EpisodicRecord,
    InMemoryEpisodicStore,
    InMemoryProcedureStore,
    InMemoryWorkingMemory,
    MemoryUnavailable,
    hit_layer,
)

__all__ = ["MemoryService", "PgDatabase", "MigrationResult", "MEMORY_SCHEMA_FILE"]

logger = logging.getLogger("agent.memory.service")

MEMORY_SCHEMA_FILE: Final[str] = "schema.sql"
#: 影子回归集大小（用库内最近 N 条自洽回归）
SHADOW_SET_SIZE: Final[int] = 10


@dataclass(frozen=True)
class MigrationResult:
    statements: int
    extensions: tuple[str, ...]


# ======================================================================
#  Postgres 连接（Agent 自己的库连接；与工具层解耦）
# ======================================================================
@dataclass
class PgDatabase:
    """薄的 asyncpg 封装：`fetch / fetchval / execute`（可注入假的做离线测试）。

    ★ 与工具层的 `SqlSource` 分开：那个属于"读被观测系统的契约表"（只读角色），
      这个属于"Agent 自己的记忆表"（要建表/更新/删除）。**权限模型不同**，
      合成一个会让人分不清"这段代码用的是哪个角色"。
    """

    dsn: str = ""
    _pool: Any = field(default=None, init=False, repr=False)
    #: ★ 建池必须串行化：`recall` 一次 `gather` 三路召回，冷启动时
    #:   三个协程会**同时**看到 `_pool is None` 并各自 `create_pool` ——
    #:   除最后一个外全部泄漏（没人 close，连接数随并发上升）。
    #:   锁**惰性创建**：不在 import/构造期绑定事件循环。
    _lock: Any = field(default=None, init=False, repr=False)
    timeout_sec: float = 10.0

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            # 双重检查：等锁期间别人可能已经把池建好了（这就是本修复的目的）
            if self._pool is None:
                import asyncpg  # noqa: PLC0415

                try:
                    self._pool = await asyncpg.create_pool(
                        dsn=self.dsn, min_size=1, max_size=4, timeout=self.timeout_sec)
                except Exception as exc:  # noqa: BLE001
                    raise MemoryUnavailable(
                        "postgres", f"连不上 Postgres：{type(exc).__name__}: {exc}") from exc
        return self._pool

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        try:
            rows = await pool.fetch(sql, *args)
        except Exception as exc:  # noqa: BLE001
            raise MemoryUnavailable("postgres", f"查询失败（{type(exc).__name__}）：{exc}") from exc
        return [dict(row) for row in rows]

    async def fetchval(self, sql: str, *args: Any) -> Any:
        pool = await self._get_pool()
        try:
            return await pool.fetchval(sql, *args)
        except Exception as exc:  # noqa: BLE001
            raise MemoryUnavailable("postgres", f"查询失败（{type(exc).__name__}）：{exc}") from exc

    async def execute(self, sql: str, *args: Any) -> str:
        pool = await self._get_pool()
        try:
            return str(await pool.execute(sql, *args))
        except Exception as exc:  # noqa: BLE001
            raise MemoryUnavailable("postgres", f"执行失败（{type(exc).__name__}）：{exc}") from exc

    async def close(self) -> None:
        if self._pool is not None:
            await close_pool(self._pool)
            self._pool = None


# ======================================================================
#  MemoryService
# ======================================================================
#: 逐值脱敏的**最大嵌套深度**（超过就 fail-closed：不保留内容，见 `_redact_remediation`）。
#: 6 层对"处置步骤"这种结构足够深（实测正常形态是 1~2 层）。
MAX_REDACT_DEPTH: Final[int] = 6
#: 深度用尽 / 循环引用的**结构化**哨兵。
#: ★★ 别退回成**中文说明字符串**：那会占住原本放容器/字符串的位置，类型不稳定
#:   （下游按类型渲染会拿到一个"看着像正文"的字符串，分不清它是被丢弃
#:   的标记还是真实内容）。现在统一成带 `_redacted` 标记的映射：**容器位置放容器**，
#:   消费方可以按 `_redacted` 明确判断"这里本来有内容、被有意丢弃了"。
_REDACT_TOO_DEEP: Final[Mapping[str, str]] = {"_redacted": "too_deep"}
_REDACT_CYCLIC: Final[Mapping[str, str]] = {"_redacted": "cyclic"}
#: 非 Mapping 入参的保留键（以前是**静默变成 `{}`**，内容无声消失）
RAW_KEY: Final[str] = "_raw"

# ----------------------------------------------------------------------
#  处置文本脱敏的四条口径（函数体只放实现）
# ----------------------------------------------------------------------
#  ★ 值按模式表抹掉（`kubectl ... token=xxx`）；键名**保留** —— `command` 是结构化
#    字段，下游按它渲染（处置步骤常写成 `{"steps": [...]}`，嵌套容器逐层过）。
#  ★★ **键名命中即整值打码**。模式表只能抓"值里带 `token=…`"的
#    形态，抓不到 `{"token": "ghp_…"}` / `{"DB_PASSWORD": "s3cr3t"}` —— 而这类写法
#    在处置步骤里很常见，凭据会被原样写进情景记忆（Postgres JSONB，**长期保存、
#    还会被召回读回**），比日志出口（`sdk/redact` 会打码）**更弱**。现在两边统一。
#  ★★ `gates.redact_text` 返回 `(文本, 命中数)`
#    **元组** —— 直接返回它会让每个字符串落库成 `["文本", 1]`：脱敏做了，**数据被写坏**。
#    → 取第 0 项，且**只接受 `str`**（拿到的不是字符串就如实报错）。
#  ★★ 深度用尽时**不再原样返回容器**（fail-closed）。以前 `depth <= 0` 直接
#    `return value`，于是**穿过两层容器**的凭据照样入库（实测
#    `{"list3": [[{"x": "token=abc123"}]]}` 原样保留 —— dict→dict→str 恰好过得去，
#    list→list→dict 就漏了：看上去"脱敏做了"其实没做）。


def _redact_value(value: Any, depth: int = MAX_REDACT_DEPTH,
                  path: frozenset[int] = frozenset()) -> Any:
    """递归脱敏一个值：字符串过模式表、敏感键整值打码、容器逐层、深度/循环 fail-closed。"""
    from .gates import REDACT_MASK, is_sensitive_key, redact_text  # noqa: PLC0415

    if isinstance(value, str):
        cleaned, _count = redact_text(value)
        if not isinstance(cleaned, str):            # pragma: no cover - 契约护栏
            raise TypeError(f"脱敏结果必须是 str，实际 {type(cleaned).__name__}")
        return cleaned
    if isinstance(value, (Mapping, list, tuple)):
        if depth <= 0:
            return _REDACT_TOO_DEEP
        if id(value) in path:                       # 循环引用：不能无限递归
            return _REDACT_CYCLIC
        nested = path | {id(value)}
        if isinstance(value, Mapping):
            # ★ 键名敏感 → **整值打码**（键名保留，见上方口径 #2）
            return {key: (REDACT_MASK if is_sensitive_key(key)
                          else _redact_value(item, depth - 1, nested))
                    for key, item in value.items()}
        return type(value)(_redact_value(item, depth - 1, nested) for item in value)
    return value


def _redact_remediation(raw: Any) -> dict[str, Any]:
    """处置文本的脱敏（四条口径见本节头部注释）。"""
    from .gates import REDACT_MASK, is_sensitive_key  # noqa: PLC0415

    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        # ★★ 以前**静默变成 `{}`** —— 内容无声消失，而读侧
        #   （`stores/episodic.py`）**专门**支持 `remediation` 是字符串的形态。
        #   现在保留成 `{"_raw": …}`（照样脱敏），并**记一条 warning**。
        logger.warning("remediation 不是映射（%s）→ 按 %r 保留并脱敏",
                       type(raw).__name__, RAW_KEY)
        return {RAW_KEY: _redact_value(raw)}
    return {str(key): (REDACT_MASK if is_sensitive_key(key)
                       else _redact_value(value))
            for key, value in raw.items()}


class MemoryService:
    """四层记忆的统一入口（依赖全部可注入）。"""

    def __init__(
        self,
        *,
        episodic: Any = None,
        working: Any = None,
        procedural: Any = None,
        semantic: Any = None,
        embedder: Optional[Embedder] = None,
        reranker: Any = None,
        rewriter: Any = None,
        per_path_k: int = 8,
    ) -> None:
        self.episodic = episodic if episodic is not None else InMemoryEpisodicStore()
        self.working = working if working is not None else InMemoryWorkingMemory()
        self.procedural = procedural if procedural is not None else InMemoryProcedureStore()
        self.semantic = semantic
        self.embedder: Embedder = embedder or LexicalHashEmbedder()
        # ★★ 把"当前 embedder 的名字"绑给 SQL 存储：
        #   写入时记的是 `embedder.name`，检索时的过滤条件必须是**同一个名字**，
        #   否则换 embedder（或将来换真模型）后向量路会**零行返回且不报错** ——
        #   静默丢掉一整条召回路。这里绑一次，读写两侧从此同源。
        self.embedder_name = str(getattr(self.embedder, "name", "") or "")
        for store in (self.episodic, self.procedural):
            binder = getattr(store, "bind_embedder", None)
            if callable(binder) and self.embedder_name:
                binder(self.embedder_name)
        self.recall_pipeline = MemoryRecall(
            episodic=self.episodic, embedder=self.embedder, procedural=self.procedural,
            semantic=self.semantic, reranker=reranker, rewriter=rewriter,
            per_path_k=per_path_k,
        )
        self.session = SessionMemory(self.working)

    # ------------------------------------------------------------------
    #  读取：六步
    # ------------------------------------------------------------------
    async def recall(self, slots: Optional[Slots] = None, symptom: Optional[str] = None,
                     *, top_k: int = 3, tenant_id: str = "default") -> list[MemoryHit]:
        """`recall(slots, symptom, top_k=3) -> list[MemoryHit]` 的入口签名。"""
        result = await self.recall_detailed(slots=slots, symptom=symptom, top_k=top_k,
                                            tenant_id=tenant_id)
        # ★ 命中要计入 hit_count（"这个案例又用上了一次"是可观测的有用信号）
        #   ★ 只有**情景记忆**有 `hit_count` 这一列：程序性/语义两层各走自己的反馈路径。
        #     判据统一走 `hit_layer()`（以前写的一端漏前缀、读的一端判错）。
        for hit in result.hits:
            if hit_layer(hit.memory_id) != "episodic":
                continue
            try:
                await self.episodic.bump_hit(hit.memory_id)
            except Exception:  # noqa: BLE001 —— 记账失败不该影响召回
                logger.warning("hit_count 更新失败：%s", hit.memory_id)
        return list(result.hits)

    async def recall_detailed(self, slots: Optional[Slots] = None,
                              symptom: Optional[str] = None, *, top_k: int = 3,
                              tenant_id: str = "default") -> RecallResult:
        """同上，但返回**完整过程**（RRF 分、各分项、降级路）—— 排障与评测用。"""
        from ..observed.context import current
        scope=current()
        if scope:tenant_id=scope.namespace
        return await self.recall_pipeline.run(slots=slots, symptom=symptom, top_k=top_k,
                                              tenant_id=tenant_id)

    # ------------------------------------------------------------------
    #  写入：五道闸门（**绝不抛异常**）
    # ------------------------------------------------------------------
    async def write(self, incident: Mapping[str, Any],
                    board: Optional[EvidenceBoard] = None, *,
                    tenant_id: str = "default") -> WriteResult:
        """把一次已结束的故障写进情景记忆。

        `incident` 至少要有 `symptom_text` / `root_cause` / `verification`；
        可选 `category` / `entity_id` / `remediation` / `review` / `occurred_at`。
        `board` 有就给：**根因置信度取自它**（`confirmed_hypothesis().score`），
        这样"分数从哪来"只有一处；没有就退回 `incident["confidence"]`。
        """
        try:
            from ..observed.context import current,body_scope
            scope=current()
            if scope:
                tenant_id=scope.namespace
                if body_scope(incident) not in (None,scope.observed_id):return WriteResult(skipped=True,reason='系统归属不符，未写入')
            return await self._write_inner(incident, board, tenant_id=tenant_id)
        except MemoryUnavailable as exc:
            logger.warning("记忆写入失败（存储不可用）：%s", exc.message)
            return WriteResult(skipped=True, reason=f"存储不可用，未写入：{exc.message}")
        except Exception as exc:  # noqa: BLE001 —— `write()` 不得抛异常
            logger.exception("记忆写入出现未预期异常")
            return WriteResult(skipped=True,
                               reason=f"写入异常（{type(exc).__name__}），未写入：{exc}")

    async def _write_inner(self, incident: Mapping[str, Any],
                           board: Optional[EvidenceBoard], *, tenant_id: str) -> WriteResult:
        symptom_text = str(incident.get("symptom_text") or "").strip()
        root_cause = str(incident.get("root_cause") or "").strip()
        if not symptom_text or not root_cause:
            return WriteResult(skipped=True,
                               reason="缺 symptom_text 或 root_cause —— 记忆没有内容可写")

        score = None
        if board is not None:
            confirmed = board.confirmed_hypothesis()
            score = confirmed.score if confirmed is not None else None
        if score is None:
            score = incident.get("confidence")

        def blocked(outcome: Any, *, memory_id: Optional[str] = None) -> WriteResult:
            """★ 被拦下时**必须写明是哪一道闸门** —— 否则读的人只能靠猜
            （"为什么这条没进记忆"是复盘里最常问的问题之一）。"""
            return WriteResult(memory_id=memory_id, skipped=True,
                               reason=f"{outcome.gate}：{outcome.reason}")

        # ---- ① 验证方式 ----
        first = gate_verification(incident.get("verification") or VerificationMethod.UNVERIFIED)
        if not first.passed:
            return blocked(first)

        # ---- ② 根因置信度 ----
        second = gate_root_cause(score)
        if not second.passed:
            return blocked(second)

        # ---- ③ 脱敏（必须在算相似度之前：否则脱敏前后被当成两条）----
        # ★ ③ **只做变换、不做拦截**：`gate_redaction` 恒 `passed=True`
        #   （"经过脱敏处理"就算通过，不是"必须发现敏感信息"）。
        #   所以这里**没有** `if not third.passed: return blocked(third)` ——
        #   那一行曾经在，但永远不成立（死守卫）。若将来要它拦（例如"脱敏没跑成"），
        #   必须在这里把判据补回来，不能只改 `gates.py` 就以为拦住了。
        third = gate_redaction(symptom_text, root_cause,
                               str(incident.get("review") or "") or None)
        clean_symptom, clean_cause, clean_review = (list(third.payload["texts"]) + [""])[:3]
        if third.payload["redacted_count"]:
            logger.info("记忆写入：脱敏抹掉 %d 处敏感片段", third.payload["redacted_count"])

        embedding = self.embedder.embed(f"{clean_symptom} {clean_cause}")

        # ---- ④ 去重合并 ----
        existing_id, similarity = await self.episodic.max_similarity(embedding, tenant_id=tenant_id)
        fourth = gate_dedupe(existing_id, similarity)
        if not fourth.passed:
            merged_into = str(fourth.payload.get("merged_into") or "")
            if merged_into:
                await self.episodic.bump_hit(merged_into)
            return blocked(fourth, memory_id=merged_into or None)

        # ---- ⑤ 影子回归（写入前基线 → 写入 → 写入后复测 → 退化则回滚）----
        shadow = await self._shadow_queries(tenant_id=tenant_id)
        before = await self._shadow_top1_rate(shadow, tenant_id=tenant_id)

        memory_id = f"mem_{uuid.uuid4().hex[:16]}"
        record = EpisodicRecord(
            memory_id=memory_id,
            symptom_text=clean_symptom,
            root_cause=clean_cause,
            verification=VerificationMethod(str(incident.get("verification"))),
            confidence=float(score or 0.0),
            category=(str(incident["category"]) if incident.get("category") else None),
            entity_id=(str(incident["entity_id"]) if incident.get("entity_id") else None),
            occurred_at=incident.get("occurred_at") or datetime.now(timezone.utc),
            # ★★ `remediation` 是**处置步骤/命令文本**（可含 DSN/token），
            #   而它以前**原样入库** —— 同一份敏感信息只有 symptom/root_cause/review
            #   三个出口过了闸门③，这与"脱敏后才落盘"的意图直接矛盾。
            #   → 按同一套模式表逐值脱敏（键名保留：那是结构化字段，不是敏感值）。
            remediation=_redact_remediation(incident.get("remediation")),
            review=clean_review or None,
            embedding=embedding,
            # ★ 写入侧记的是**注入的 embedder 自报的名字**（检索侧绑的也是它）
            embedding_model=self.embedder_name or LEXICAL_EMBEDDER_NAME,
            batch_id=f"batch_{datetime.now(timezone.utc).strftime('%Y%m%d')}",
            tenant_id=tenant_id,
        )
        await self.episodic.insert(record)

        after = await self._shadow_top1_rate(shadow, tenant_id=tenant_id)
        fifth = gate_shadow_regression(before, after)
        if not fifth.passed:
            await self.episodic.delete(memory_id)      # ★ 熔断 = 回滚，不留半成品
            return blocked(fifth, memory_id=memory_id)

        report = GateReport(outcomes=(first, second, third, fourth, fifth))
        return WriteResult(memory_id=memory_id, batch_id=record.batch_id, skipped=False,
                           reason=f"五道闸门通过（{report.reason() or '无告警'}）")

    # ------------------------------------------------------------------
    async def _shadow_queries(self, *, tenant_id: str) -> list[EpisodicRecord]:
        """影子集：库里最近的 N 条（近似做法；将来换人工标注集）。"""
        return await self.episodic.recent(limit=SHADOW_SET_SIZE, tenant_id=tenant_id)

    async def _shadow_top1_rate(self, shadow: Sequence[EpisodicRecord], *,
                                tenant_id: str) -> float:
        """Top-1 命中率：用每条影子记忆自己的症状去查，看第一名是不是**它自己**。"""
        if not shadow:
            return 0.0
        hits = 0
        for record in shadow:
            try:
                found = await self.episodic.search_vector(
                    record.embedding, top_k=1, tenant_id=tenant_id)
            except MemoryUnavailable:
                raise
            if found and found[0][0].memory_id == record.memory_id:
                hits += 1
        return hits / len(shadow)

    # ------------------------------------------------------------------
    #  反馈
    # ------------------------------------------------------------------
    async def feedback(self, memory_id: str, *, useful: bool,
                       tenant_id: str = "default") -> None:
        """更新 `hit_count` 与衰减。SOP 额外更新成功率（按成功率自我进化）。"""
        try:
            if hit_layer(memory_id) == "procedure":
                # ★ 必须剥掉前缀再交给 store —— 前缀是**命中层的判据**，
                #   不是 procedure_id 的一部分。
                await self.procedural.feedback(
                    memory_id[len(PROCEDURE_ID_PREFIX):], useful=useful)
            elif useful:
                await self.episodic.bump_hit(memory_id)
        except Exception as exc:  # noqa: BLE001 —— 反馈失败不该影响调用方
            logger.warning("记忆反馈失败（%s）：%s", memory_id, exc)

    # ------------------------------------------------------------------
    #  迁移
    # ------------------------------------------------------------------
    async def migrate(self, db: PgDatabase) -> MigrationResult:
        """建表（幂等）：读 `schema.sql` 逐句执行。

        ★ 记忆表**不属于**存储契约的契约表范围（自行设计并迁移），
          所以 DDL 放在 Agent 侧、由这条命令负责建 —— 而不是塞进
          `deploy/postgres/init/`（那里是契约表的初始化）。
        """
        from pathlib import Path

        sql_text = (Path(__file__).with_name(MEMORY_SCHEMA_FILE)).read_text(encoding="utf-8")
        statements = [stmt.strip() for stmt in sql_text.split(";") if stmt.strip()
                      and not all(line.strip().startswith("--") or not line.strip()
                                  for line in stmt.splitlines())]
        extensions: list[str] = []
        for statement in statements:
            await db.execute(statement)
            head = statement.split("(")[0].strip().upper()
            if "CREATE EXTENSION" in head:
                extensions.append(statement.split()[-1].strip('"'))
        return MigrationResult(statements=len(statements), extensions=tuple(extensions))
