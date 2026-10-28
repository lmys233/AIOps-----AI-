-- ==================================================================
--  拓扑表（T18c）—— **Agent 自己的表，不在 spec §8.6 契约内**
--
--  spec §8.6 只覆盖"被观测系统与 Agent 共用的表"（change_events / audit_log /
--  approval_requests）。服务与依赖边是 **Agent 侧的派生数据**（从被观测系统
--  自报或部署配置派生），所以 DDL 归 Agent 自己，由
--  `.\run.ps1 topology-migrate` 幂等迁移 —— 与记忆层的做法一致。
--
--  ★ 为什么用两张表而不是一张图数据库：P1 的规模是"十来个服务 + 几十条边"，
--    Postgres 两张表 + 一次全量读入内存做 BFS 足够；决策记录 B3 已经把
--    切到 Neo4j 的路径留下了（接口是 `TopologyStore`，不含存储细节）。
-- ==================================================================

CREATE TABLE IF NOT EXISTS topology_services (
    -- ★ 规范实体键：有集群时 `svc:order@compose-cn-1`；
    --   **没有集群时允许写成 `svc:order`**（单集群 / 未自报的场景）。
    --   集群名是"限定符"，不是"定位符" —— 定位靠服务名（见 store.resolve）。
    entity_id       TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    cluster         TEXT,                       -- 可空：没拿到就是 NULL，**不发明值**
    env             TEXT NOT NULL DEFAULT 'prod',
    namespace       TEXT,
    url             TEXT,
    alive           BOOLEAN NOT NULL DEFAULT TRUE,
    owner           TEXT,
    tier            INTEGER,
    -- ★ 这个身份是**从哪来的**：self_report / repo:app.yaml(dev) / manual …
    --   不记来源的话，"自报的"与"从配置文件抄的"以后分不出来。
    identity_source TEXT NOT NULL DEFAULT 'unknown',
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 按名字查（解析 `order` / `svc:order` 这类写法时用）
CREATE INDEX IF NOT EXISTS topology_services_name_idx ON topology_services (name);
CREATE INDEX IF NOT EXISTS topology_services_cluster_idx ON topology_services (cluster);

CREATE TABLE IF NOT EXISTS topology_edges (
    from_entity_id  TEXT NOT NULL,
    to_entity_id    TEXT NOT NULL,
    call_type       TEXT NOT NULL DEFAULT 'http',
    criticality     TEXT NOT NULL DEFAULT 'medium',
    -- 这条边从哪来（同上：来源必须落在数据里）
    source          TEXT NOT NULL DEFAULT 'unknown',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (from_entity_id, to_entity_id)
);

CREATE INDEX IF NOT EXISTS topology_edges_to_idx ON topology_edges (to_entity_id);
