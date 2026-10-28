-- ==================================================================
--  Agent 记忆层的表（spec §3.3 四层记忆）
--
--  ★★ 这些表**不属于** spec §8.6 的存储契约（那是"被观测系统 + 三条契约表"）。
--     它们是 **Agent 实现内部事务** —— task.md T17 细节 1 明确要求
--     "需自行设计并迁移"。所以这份 DDL 放在 Agent 侧（本文件），
--     而不是 `deploy/postgres/init/`（那里的 01-schema.sql 是契约表）。
--
--  ★ 迁移方式：`python -m agent.memory --migrate`（幂等，全是 IF NOT EXISTS）
--
--  ★ 为什么第 4 层（程序性）与第 2 层（情景）都带向量：
--    spec §3.3 说程序性记忆"按成功率自我进化" —— 它也要能被"这次现象像不像那条套路"召回。
-- ==================================================================

CREATE EXTENSION IF NOT EXISTS vector;      -- pgvector：情景/程序性记忆的向量路
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- 关键词路（决策记录 B2：P1 用 pg_trgm + GIN）

-- ----------------------------------------------------------------
--  ① 情景记忆（episodic）：历史故障 现象→证据→根因→处置→复盘
--     spec §3.3：长期存储、**带时间衰减**、只在故障关闭后写（5 道闸门）
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_memory_episodic (
    memory_id       TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL DEFAULT 'default',

    -- 检索用的三段文本（查询改写后的 symptom 也存一份，便于回归集复用）
    symptom_text    TEXT NOT NULL,
    root_cause      TEXT NOT NULL,
    category        TEXT,                       -- config_change / capacity / dependency / code …

    -- 实体（规范化 `type:name@cluster`，与 §7.1 的 EntityRef 一致）
    entity_id       TEXT,
    entity_type     TEXT,
    cluster         TEXT,

    -- ★ 用于时间衰减（spec §7.4 的 `0.20*decay`）：故障发生时刻
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- 验证方式与置信度：写入闸门 ① ② 直接读它们
    verification    TEXT NOT NULL,
    confidence      DOUBLE PRECISION NOT NULL DEFAULT 0.0,

    -- 处置与复盘（§3.3：历史故障的完整闭环）
    remediation     JSONB,
    review          TEXT,

    -- 命中与取代：注入上下文时必须带这两个元数据（§7.4）
    hit_count       INTEGER NOT NULL DEFAULT 0,
    superseded_by   TEXT REFERENCES agent_memory_episodic(memory_id),

    -- 向量路
    embedding       vector(256),
    -- ★ 换过 embedding 模型的老向量不可比，必须能按它过滤（见 embedding.py 的说明）
    embedding_model TEXT NOT NULL DEFAULT 'lexical-hash-ngram-v1',

    batch_id        TEXT
);

-- 关键词路：pg_trgm 的 GIN 索引（`similarity()` 走它）
CREATE INDEX IF NOT EXISTS idx_agent_mem_trgm
    ON agent_memory_episodic USING gin (symptom_text gin_trgm_ops);
-- 结构化路：按实体 + 时间
CREATE INDEX IF NOT EXISTS idx_agent_mem_entity
    ON agent_memory_episodic (entity_id, occurred_at DESC);
-- 召回时通常只看未被取代的（superseded_by IS NULL）
CREATE INDEX IF NOT EXISTS idx_agent_mem_live
    ON agent_memory_episodic (tenant_id, occurred_at DESC)
    WHERE superseded_by IS NULL;
-- 向量路：HNSW（数据量小的时候用不上，但结构先留好）
CREATE INDEX IF NOT EXISTS idx_agent_mem_vec
    ON agent_memory_episodic USING hnsw (embedding vector_cosine_ops);

-- ----------------------------------------------------------------
--  ② 程序性记忆（procedural）：排查套路 / SOP，**按成功率自我进化**
-- ----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_memory_procedures (
    procedure_id    TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL DEFAULT 'default',
    name            TEXT NOT NULL,
    symptom_text    TEXT NOT NULL,              -- 什么时候该用它
    steps           JSONB NOT NULL,             -- [{order, action, tool, expect}]
    success_rate    DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    used_count      INTEGER NOT NULL DEFAULT 0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    embedding       vector(256),
    embedding_model TEXT NOT NULL DEFAULT 'lexical-hash-ngram-v1'
);

CREATE INDEX IF NOT EXISTS idx_agent_proc_trgm
    ON agent_memory_procedures USING gin (symptom_text gin_trgm_ops);

-- ----------------------------------------------------------------
--  ③ 语义记忆（semantic）：服务拓扑 / Owner / SLO
--     spec §3.3 说它"随 CMDB 同步"，P1 由 `TopologyStore`（T4 接口）承载 ——
--     这里**不建表**：那是被观测系统的服务目录，不该由 Agent 记忆层再造一份。
--     （"同一事实只留一份"：拓扑的唯一来源是 TopologyStore。）
-- ----------------------------------------------------------------

-- ----------------------------------------------------------------
--  ④ 工作记忆（working）：会话级黑板 / 时间轴 / 澄清记账
--     spec §3.3：Redis，会话级 TTL 24h —— 所以**这里没有它的表**。
--     它只在 Redis 里（见 stores.py 的 WorkingMemory）。
-- ----------------------------------------------------------------

-- 影子回归（写入闸门 ⑤）要能快速取"最近 N 条"做回归集
CREATE INDEX IF NOT EXISTS idx_agent_mem_recent
    ON agent_memory_episodic (created_at DESC);
