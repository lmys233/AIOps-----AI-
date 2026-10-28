"""情景记忆（历史故障，带时间衰减；内存 + Postgres/pgvector 两种实现）。

★ 本文件由原先的单文件 `stores.py`（903 行）拆分而来；
  **公开导入面一字未改**（包的 `__init__.py` 显式再导出）。
"""

from __future__ import annotations

from ...common.enums import VerificationMethod
from ..embedding import LEXICAL_EMBEDDER_NAME, cosine_similarity
from typing import Any, Final, Mapping, Optional, Protocol, Sequence, runtime_checkable

from dataclasses import dataclass, field
import json
from .base import _as_dt, _keyword_score, _now, _vec_literal, logger
from .records import EpisodicRecord


# ======================================================================
#  ② 情景记忆
# ======================================================================
@runtime_checkable
class EpisodicStore(Protocol):
    """情景记忆存储。三路召回各一个方法 —— 并行调用是 `recall.py` 的事。"""

    async def insert(self, record: EpisodicRecord) -> str: ...
    async def get(self, memory_id: str) -> Optional[EpisodicRecord]: ...
    async def search_vector(self, embedding: Sequence[float], *, top_k: int,
                            tenant_id: str = "default") -> list[tuple[EpisodicRecord, float]]: ...
    async def search_keyword(self, text: str, *, top_k: int,
                             tenant_id: str = "default") -> list[tuple[EpisodicRecord, float]]: ...
    async def search_structured(self, *, entity_id: Optional[str], symptom: str, top_k: int,
                                tenant_id: str = "default") -> list[tuple[EpisodicRecord, float]]: ...
    async def max_similarity(self, embedding: Sequence[float], *,
                             tenant_id: str = "default") -> tuple[Optional[str], float]: ...
    async def recent(self, *, limit: int, tenant_id: str = "default") -> list[EpisodicRecord]: ...
    async def bump_hit(self, memory_id: str) -> None: ...
    async def mark_superseded(self, old_id: str, new_id: str) -> None: ...
    async def delete(self, memory_id: str) -> bool: ...

@dataclass
class InMemoryEpisodicStore:
    """内存情景记忆（离线单测；逻辑与 SQL 版**同构**：三路各按自己的判据排序）。"""

    records: list[EpisodicRecord] = field(default_factory=list)
    clock: Any = _now

    async def insert(self, record: EpisodicRecord) -> str:
        if any(r.memory_id == record.memory_id for r in self.records):
            raise ValueError(f"memory_id 重复：{record.memory_id}")
        self.records.append(record)
        return record.memory_id

    async def get(self, memory_id: str) -> Optional[EpisodicRecord]:
        return next((r for r in self.records if r.memory_id == memory_id), None)

    def _live(self, tenant_id: str) -> list[EpisodicRecord]:
        return [r for r in self.records if r.superseded_by is None and r.tenant_id == tenant_id]

    async def search_vector(self, embedding: Sequence[float], *, top_k: int,
                            tenant_id: str = "default") -> list[tuple[EpisodicRecord, float]]:
        scored = [(r, cosine_similarity(embedding, r.embedding)) for r in self._live(tenant_id)
                  if len(r.embedding) == len(embedding)]
        return sorted(scored, key=lambda pair: pair[1], reverse=True)[:top_k]

    async def search_keyword(self, text: str, *, top_k: int,
                             tenant_id: str = "default") -> list[tuple[EpisodicRecord, float]]:
        scored = [(r, _keyword_score(text, r.symptom_text)) for r in self._live(tenant_id)]
        return sorted(scored, key=lambda pair: pair[1], reverse=True)[:top_k]

    async def search_structured(self, *, entity_id: Optional[str], symptom: str, top_k: int,
                                tenant_id: str = "default") -> list[tuple[EpisodicRecord, float]]:
        """结构化路：**实体精确匹配优先**，其次按时间新旧。

        ★ 这一路的判据与文本无关（结构化过滤是"硬条件"），
          所以它常能捞到"文本不像、但确实是同一个实体"的历史案例。
        """
        hits: list[tuple[EpisodicRecord, float]] = []
        for record in self._live(tenant_id):
            if entity_id and record.entity_id == entity_id:
                hits.append((record, 1.0))
            elif entity_id and record.entity_id and record.entity_id.endswith(
                    "@" + (entity_id.split("@")[-1] if "@" in entity_id else "")
            ):
                hits.append((record, 0.5))
        hits.sort(key=lambda pair: (pair[1], pair[0].occurred_at), reverse=True)
        return hits[:top_k]

    async def max_similarity(self, embedding: Sequence[float], *,
                             tenant_id: str = "default") -> tuple[Optional[str], float]:
        best: tuple[Optional[str], float] = (None, 0.0)
        for record in self._live(tenant_id):
            if len(record.embedding) != len(embedding):
                continue
            score = cosine_similarity(embedding, record.embedding)
            if score > best[1]:
                best = (record.memory_id, score)
        return best

    async def recent(self, *, limit: int, tenant_id: str = "default") -> list[EpisodicRecord]:
        live = sorted(self._live(tenant_id), key=lambda r: r.created_at, reverse=True)
        return live[:limit]

    async def bump_hit(self, memory_id: str) -> None:
        for index, record in enumerate(self.records):
            if record.memory_id == memory_id:
                self.records[index] = EpisodicRecord(**{**record.__dict__,
                                                        "hit_count": record.hit_count + 1})
                return

    async def mark_superseded(self, old_id: str, new_id: str) -> None:
        for index, record in enumerate(self.records):
            if record.memory_id == old_id:
                self.records[index] = EpisodicRecord(**{**record.__dict__,
                                                        "superseded_by": new_id})
                return

    async def delete(self, memory_id: str) -> bool:
        before = len(self.records)
        self.records = [r for r in self.records if r.memory_id != memory_id]
        return len(self.records) != before

def _parse_vector(raw: Any) -> tuple[float, ...]:
    """把 pgvector 的文本形态 `[0.1,0.2,…]` 解析回元组。"""
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple)):
        return tuple(float(value) for value in raw)
    text = str(raw).strip()
    if not text or text == "[]":
        return ()
    inner = text[1:-1] if text.startswith("[") else text
    try:
        return tuple(float(part) for part in inner.split(",") if part.strip())
    except ValueError:
        logger.warning("向量文本解析失败（已当空向量处理）：%.40s", text)
        return ()

def _row_to_record(row: Mapping[str, Any], *, with_embedding: bool = False,
                   default_model: str = LEXICAL_EMBEDDER_NAME) -> EpisodicRecord:
    """行 → 记录。

    ★ `default_model` 由**调用方**（store）传入，而不是在这里写常量：
      这一列只在"老数据没写模型名"时才用得上，用错名字会让它被**错误的过滤条件**匹配。
    """
    remediation = row.get("remediation")
    if isinstance(remediation, str):
        try:
            remediation = json.loads(remediation)
        except ValueError:
            remediation = {"_raw": remediation}
    return EpisodicRecord(
        memory_id=str(row["memory_id"]),
        tenant_id=str(row.get("tenant_id") or "default"),
        symptom_text=str(row.get("symptom_text") or ""),
        root_cause=str(row.get("root_cause") or ""),
        verification=VerificationMethod(
            str(row.get("verification") or VerificationMethod.UNVERIFIED.value)),
        confidence=float(row.get("confidence") or 0.0),
        category=(str(row["category"]) if row.get("category") else None),
        entity_id=(str(row["entity_id"]) if row.get("entity_id") else None),
        entity_type=(str(row["entity_type"]) if row.get("entity_type") else None),
        cluster=(str(row["cluster"]) if row.get("cluster") else None),
        occurred_at=_as_dt(row.get("occurred_at")),
        created_at=_as_dt(row.get("created_at")),
        remediation=dict(remediation or {}),
        review=(str(row["review"]) if row.get("review") else None),
        hit_count=int(row.get("hit_count") or 0),
        superseded_by=(str(row["superseded_by"]) if row.get("superseded_by") else None),
        embedding=_parse_vector(row.get("embedding_text")) if with_embedding else (),
        embedding_model=str(row.get("embedding_model") or default_model),
        batch_id=(str(row["batch_id"]) if row.get("batch_id") else None),
    )

def _require_vector(embedding: Sequence[float], *, where: str) -> str:
    """向量为空时**说清原因**，而不是把 `[]` 送给 pgvector 换一句天书错误。

    ★ pgvector 对空向量的报错是 `vector must have at least 1 dimension` ——
      看到它的人第一反应会是"模型输出维度不对"，而真实原因通常是
      "这条记录是从**不带向量的查询**里读出来的"。
    """
    if not embedding:
        raise ValueError(
            f"{where}: 向量为空 —— 常见原因是**记录来自不带向量的查询**"
            f"（`_EPISODIC_COLUMNS` 不含 embedding；需要向量请用 "
            f"`recent()` / `get()` 这类带向量的读取）"
        )
    return _vec_literal(embedding)

@dataclass
class SqlEpisodicStore:
    """Postgres + pgvector 实现（Agent 自建表，见 `schema.sql`）。

    ★ `db` 需提供 `fetch / fetchval / execute`（见 `service.PgDatabase` 与测试里的假实现）。

    ★★ `embedder_name` 必须与**写入时**用的 embedder 一致：
      `embedding_model` 这一列存在的意义就是"换过模型的老向量必须被过滤掉"，
      而过滤条件写死常量等于把它废掉 —— 注入任何 `.name` 不同的 embedder
      （或换个真模型）后，`search_vector` / `max_similarity` 会**零行返回且不报错**，
      召回静默丢掉一整条路径。所以过滤用 `self.embedder_name`，
      由 `MemoryService` 在装配时绑定（它同时看得见 embedder 与 store）。
    """

    db: Any
    embedder_name: str = LEXICAL_EMBEDDER_NAME

    def bind_embedder(self, name: str) -> None:
        """绑定"当前 embedder 的名字"（过滤条件与写入侧从此同源）。"""
        self.embedder_name = str(name or LEXICAL_EMBEDDER_NAME)

    async def insert(self, record: EpisodicRecord) -> str:
        await self.db.execute(
            f"""INSERT INTO agent_memory_episodic ({_EPISODIC_COLUMNS}, embedding)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14,$15,$16,$17,$18,
                        $19::vector)""",
            record.memory_id, record.tenant_id, record.symptom_text, record.root_cause,
            record.category, record.entity_id, record.entity_type, record.cluster,
            record.occurred_at, record.created_at, record.verification.value,
            record.confidence, json.dumps(record.remediation, ensure_ascii=False),
            record.review, record.hit_count, record.superseded_by,
            record.embedding_model, record.batch_id,
            _vec_literal(record.embedding) if record.embedding else None,
        )
        return record.memory_id

    async def get(self, memory_id: str) -> Optional[EpisodicRecord]:
        rows = await self.db.fetch(
            f"SELECT {_EPISODIC_COLUMNS_WITH_VEC} FROM agent_memory_episodic "
            f"WHERE memory_id = $1",
            memory_id)
        return _row_to_record(rows[0], with_embedding=True,
                              default_model=self.embedder_name) if rows else None

    async def search_vector(self, embedding: Sequence[float], *, top_k: int,
                            tenant_id: str = "default") -> list[tuple[EpisodicRecord, float]]:
        literal = _require_vector(embedding, where="SqlEpisodicStore.search_vector")
        rows = await self.db.fetch(
            f"""SELECT {_EPISODIC_COLUMNS}, 1 - (embedding <=> $1::vector) AS similarity
                  FROM agent_memory_episodic
                 WHERE tenant_id = $2 AND superseded_by IS NULL
                   AND embedding IS NOT NULL AND embedding_model = $3
                 ORDER BY embedding <=> $1::vector
                 LIMIT $4""",
            literal, tenant_id, self.embedder_name, top_k)
        return [(_row_to_record(row, default_model=self.embedder_name),
                 float(row.get("similarity") or 0.0)) for row in rows]

    async def search_keyword(self, text: str, *, top_k: int,
                             tenant_id: str = "default") -> list[tuple[EpisodicRecord, float]]:
        rows = await self.db.fetch(
            f"""SELECT {_EPISODIC_COLUMNS}, similarity(symptom_text, $1) AS similarity
                  FROM agent_memory_episodic
                 WHERE tenant_id = $2 AND superseded_by IS NULL
                 ORDER BY similarity(symptom_text, $1) DESC
                 LIMIT $3""",
            text, tenant_id, top_k)
        return [(_row_to_record(row, default_model=self.embedder_name),
                 float(row.get("similarity") or 0.0)) for row in rows]

    async def search_structured(self, *, entity_id: Optional[str], symptom: str, top_k: int,
                                tenant_id: str = "default") -> list[tuple[EpisodicRecord, float]]:
        rows = await self.db.fetch(
            f"""SELECT {_EPISODIC_COLUMNS},
                       CASE WHEN $1::text IS NOT NULL AND entity_id = $1::text THEN 1.0 ELSE 0.4 END
                       AS similarity
                  FROM agent_memory_episodic
                 WHERE tenant_id = $2 AND superseded_by IS NULL
                   AND (
                        ($1::text IS NOT NULL AND (
                            entity_id = $1::text
                            OR split_part(entity_id, '@', 2) = split_part($1::text, '@', 2)))
                        OR similarity(symptom_text, $3) > 0.2
                   )
                 ORDER BY similarity DESC, occurred_at DESC
                 LIMIT $4""",
            entity_id, tenant_id, symptom, top_k)
        return [(_row_to_record(row, default_model=self.embedder_name),
                 float(row.get("similarity") or 0.0)) for row in rows]

    async def max_similarity(self, embedding: Sequence[float], *,
                             tenant_id: str = "default") -> tuple[Optional[str], float]:
        literal = _require_vector(embedding, where="SqlEpisodicStore.max_similarity")
        rows = await self.db.fetch(
            """SELECT memory_id, 1 - (embedding <=> $1::vector) AS similarity
                 FROM agent_memory_episodic
                WHERE tenant_id = $2 AND superseded_by IS NULL
                  AND embedding IS NOT NULL AND embedding_model = $3
                ORDER BY embedding <=> $1::vector
                LIMIT 1""",
            literal, tenant_id, self.embedder_name)
        if not rows:
            return None, 0.0
        return str(rows[0]["memory_id"]), float(rows[0].get("similarity") or 0.0)

    async def recent(self, *, limit: int, tenant_id: str = "default") -> list[EpisodicRecord]:
        """最近 N 条 —— ★ **带向量**返回（影子回归要拿它自己的向量去查它自己）。"""
        rows = await self.db.fetch(
            f"""SELECT {_EPISODIC_COLUMNS_WITH_VEC} FROM agent_memory_episodic
                 WHERE tenant_id = $1 AND superseded_by IS NULL
                 ORDER BY created_at DESC LIMIT $2""",
            tenant_id, limit)
        return [_row_to_record(row, with_embedding=True,
                               default_model=self.embedder_name) for row in rows]

    async def bump_hit(self, memory_id: str) -> None:
        await self.db.execute(
            "UPDATE agent_memory_episodic SET hit_count = hit_count + 1 WHERE memory_id = $1",
            memory_id)

    async def mark_superseded(self, old_id: str, new_id: str) -> None:
        await self.db.execute(
            "UPDATE agent_memory_episodic SET superseded_by = $2 WHERE memory_id = $1",
            old_id, new_id)

    async def delete(self, memory_id: str) -> bool:
        result = await self.db.execute(
            "DELETE FROM agent_memory_episodic WHERE memory_id = $1", memory_id)
        return str(result).endswith("1")


_EPISODIC_COLUMNS: Final[str] = (
    "memory_id, tenant_id, symptom_text, root_cause, category, entity_id, entity_type, "
    "cluster, occurred_at, created_at, verification, confidence, remediation, review, "
    "hit_count, superseded_by, embedding_model, batch_id"
)


#: **带向量**的列（把 `vector` 转成文本再取）。
#: ★ 为什么要有这第二份列清单：
#:   列表查询（三路召回）**故意不带向量** —— 256 个浮点 × 几十行是纯浪费；
#:   但**影子回归**（闸门⑤）必须拿"每条记忆自己的向量"去查它自己，
#:   没有向量就只能传空向量给 pgvector（`vector must have at least 1 dimension`）。
#:   早期版本只有一份列清单，于是**只有真链路才会炸**：内存假实现里向量一直在，
#:   SQL 读回来的记录却悄悄丢了它。
_EPISODIC_COLUMNS_WITH_VEC: Final[str] = (
    _EPISODIC_COLUMNS + ", embedding::text AS embedding_text"
)
