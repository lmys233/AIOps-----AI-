"""程序性记忆（SOP，按成功率自我进化）。

★ 本文件由原先的单文件 `stores.py`（903 行）拆分而来；
  **公开导入面一字未改**（包的 `__init__.py` 显式再导出）。
"""

from __future__ import annotations

from ..embedding import LEXICAL_EMBEDDER_NAME, cosine_similarity
from typing import Any, Optional, Protocol, Sequence, runtime_checkable

from dataclasses import dataclass, field
import json
from .base import _as_dt, _keyword_score, _now, _vec_literal, bare_procedure
from .records import ProcedureRecord


# ======================================================================
#  ④ 程序性记忆（SOP）
# ======================================================================
@runtime_checkable
class ProcedureStore(Protocol):
    async def upsert(self, record: ProcedureRecord) -> str: ...
    async def search(self, symptom: str, *, top_k: int,
                     embedding: Optional[Sequence[float]] = None,
                     tenant_id: str = "default") -> list[tuple[ProcedureRecord, float]]: ...
    async def feedback(self, procedure_id: str, *, useful: bool) -> None: ...

@dataclass
class InMemoryProcedureStore:
    records: list[ProcedureRecord] = field(default_factory=list)

    async def upsert(self, record: ProcedureRecord) -> str:
        record = bare_procedure(record)
        for index, existing in enumerate(self.records):
            if existing.procedure_id == record.procedure_id:
                self.records[index] = record
                return record.procedure_id
        self.records.append(record)
        return record.procedure_id

    async def search(self, symptom: str, *, top_k: int,
                     embedding: Optional[Sequence[float]] = None,
                     tenant_id: str = "default") -> list[tuple[ProcedureRecord, float]]:
        scored: list[tuple[ProcedureRecord, float]] = []
        for record in self.records:
            if record.tenant_id != tenant_id:
                continue
            text_score = _keyword_score(symptom, record.symptom_text)
            vec_score = (cosine_similarity(embedding, record.embedding)
                         if embedding and len(record.embedding) == len(embedding) else 0.0)
            scored.append((record, max(text_score, vec_score)))
        scored.sort(key=lambda pair: (pair[1], pair[0].success_rate), reverse=True)
        return scored[:top_k]

    async def feedback(self, procedure_id: str, *, useful: bool) -> None:
        for index, record in enumerate(self.records):
            if record.procedure_id != procedure_id:
                continue
            used = record.used_count + 1
            # ★ 成功率用**滑动平均**更新（按成功率自我进化）
            rate = (record.success_rate * record.used_count + (1.0 if useful else 0.0)) / used
            self.records[index] = ProcedureRecord(**{
                **record.__dict__, "used_count": used, "success_rate": round(rate, 4),
                "updated_at": _now()})
            return

@dataclass
class SqlProcedureStore:
    """程序性记忆（SOP）的 Postgres 实现。★ `embedder_name` 的语义同 `SqlEpisodicStore`。"""

    db: Any
    embedder_name: str = LEXICAL_EMBEDDER_NAME

    def bind_embedder(self, name: str) -> None:
        self.embedder_name = str(name or LEXICAL_EMBEDDER_NAME)

    async def upsert(self, record: ProcedureRecord) -> str:
        record = bare_procedure(record)
        await self.db.execute(
            """INSERT INTO agent_memory_procedures
                   (procedure_id, tenant_id, name, symptom_text, steps, success_rate,
                    used_count, updated_at, embedding, embedding_model)
               VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9::vector,$10)
               ON CONFLICT (procedure_id) DO UPDATE SET
                   name = EXCLUDED.name, symptom_text = EXCLUDED.symptom_text,
                   steps = EXCLUDED.steps, success_rate = EXCLUDED.success_rate,
                   used_count = EXCLUDED.used_count, updated_at = EXCLUDED.updated_at,
                   embedding = EXCLUDED.embedding,
                   -- ★★ 这一列以前漏了 —— 而 `search` 正是按它过滤。
                   --   换过 embedder 后重新 upsert 同一条 SOP 会变成
                   --   "向量是新的、模型名还是旧的" → 该行被过滤条件排除 →
                   --   **从向量路静默消失**（拿旧模型名查则用新空间的向量算余弦 → 排序错）。
                   --   这与同包 episodic 的"写入侧与过滤侧同源"是同一口径。
                   embedding_model = EXCLUDED.embedding_model""",
            record.procedure_id, record.tenant_id, record.name, record.symptom_text,
            json.dumps(list(record.steps), ensure_ascii=False), record.success_rate,
            record.used_count, record.updated_at,
            _vec_literal(record.embedding) if record.embedding else None,
            self.embedder_name)
        return record.procedure_id

    async def search(self, symptom: str, *, top_k: int,
                     embedding: Optional[Sequence[float]] = None,
                     tenant_id: str = "default") -> list[tuple[ProcedureRecord, float]]:
        rows = await self.db.fetch(
            """SELECT procedure_id, name, symptom_text, steps, success_rate, used_count,
                      updated_at,
                      GREATEST(similarity(symptom_text, $1),
                               CASE WHEN $2::vector IS NULL THEN 0
                                    ELSE 1 - (embedding <=> $2::vector) END) AS similarity
                 FROM agent_memory_procedures
                WHERE tenant_id = $3
                  AND ($2::vector IS NULL OR (embedding IS NOT NULL
                                              AND embedding_model = $4))
                ORDER BY similarity DESC, success_rate DESC
                LIMIT $5""",
            symptom, _vec_literal(embedding) if embedding else None, tenant_id,
            self.embedder_name, top_k)
        out: list[tuple[ProcedureRecord, float]] = []
        for row in rows:
            steps = row.get("steps")
            if isinstance(steps, str):
                try:
                    steps = json.loads(steps)
                except ValueError:
                    steps = []
            out.append((ProcedureRecord(
                procedure_id=str(row["procedure_id"]), name=str(row.get("name") or ""),
                symptom_text=str(row.get("symptom_text") or ""),
                steps=tuple(steps or []),
                success_rate=float(row.get("success_rate") or 0.0),
                used_count=int(row.get("used_count") or 0),
                updated_at=_as_dt(row.get("updated_at")),
            ), float(row.get("similarity") or 0.0)))
        return out

    async def feedback(self, procedure_id: str, *, useful: bool) -> None:
        await self.db.execute(
            """UPDATE agent_memory_procedures
                  SET used_count = used_count + 1,
                      success_rate = (success_rate * used_count + $2) / (used_count + 1),
                      updated_at = now()
                WHERE procedure_id = $1""",
            procedure_id, 1.0 if useful else 0.0)
