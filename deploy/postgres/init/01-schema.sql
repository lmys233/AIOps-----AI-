-- ============================================================
--  存储契约 · 初始化（严格依据 spec.md §8.6）
--
--  ★ 本文件只创建**有 spec 契约依据**的表。
--    spec §8.6 明确：「只覆盖双方共用的表；Agent 自身的记忆层表
--    （情景/语义/程序性记忆）不在本契约内，属 Agent 实现内部事务」。
--
--    因此这里**刻意不建** episodes / procedures / services /
--    service_dependencies —— 它们属 Agent 实现，应由新实现从
--    spec §3.3（记忆四层）与 §7.1（契约对象）推导，而不是从旧 SQL 抄。
--
--  本文件覆盖：
--    §8.6 change_events       变更记录（Change Agent 的唯一数据源）
--    §8.6 audit_log           审计日志（工具调用与变更留痕）
--    §8.6 approval_requests   审批令牌（L3 审批中断点）
--
--  每条 DDL 都标注 spec 出处，便于回溯"为什么有这个字段"。
-- ============================================================

-- 仅保留确实需要的扩展。记忆层向量扩展（vector）不在本契约内，
-- 待 Agent 实现记忆层时自行引入。
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================
--  1. change_events · 变更记录
--     spec §8.6 / 决策记录 A2
-- ============================================================
CREATE TABLE IF NOT EXISTS change_events (
    change_id         TEXT PRIMARY KEY,

    -- 归因时用于与指标拐点对齐（spec §8.6：与 onset 比较判断"变更早于异常"）
    changed_at        TIMESTAMPTZ NOT NULL,

    -- 格式同 EntityRef.id（spec §7.1：`type:name@cluster`）
    entity_id         TEXT NOT NULL,
    entity_type       TEXT,

    change_type       TEXT,     -- configmap / image / hpa / resource_limit / route
    operator          TEXT,
    source            TEXT,     -- gitops / manual / agent / scenario_script

    -- ★ 变更内容。结构约定见 spec §8.6：
    --   {field[], old_value[], new_value[], action_hash, scenario_id}
    --   old_value / new_value **内嵌于此，不是独立列**（决策记录 A2）
    diff              JSONB,

    git_commit        TEXT,
    rollback_ref      TEXT,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 归因查询的主要访问模式：按时间窗 + 按实体
CREATE INDEX IF NOT EXISTS idx_change_events_time
    ON change_events (changed_at DESC);
CREATE INDEX IF NOT EXISTS idx_change_events_entity
    ON change_events (entity_id, changed_at DESC);

-- ============================================================
--  2. audit_log · 审计日志
--     spec §8.6 / §2 H3 H4 / §3.4
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_log (
    audit_id          BIGSERIAL PRIMARY KEY,
    occurred_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    incident_id       TEXT,

    -- spec §8.6：格式 `agent:<role>` / `human:<name>` / `system`
    actor             TEXT NOT NULL,

    -- spec §8.6 取值集合
    action_class      TEXT NOT NULL,

    target            TEXT,

    -- 变更类动作必填（spec §8.6）
    action_hash       TEXT,
    risk_level        TEXT,          -- L0~L4
    approval_token    TEXT,

    params            JSONB,         -- 工具调用原始参数
    result_summary    TEXT,
    outcome           TEXT,
    deny_reason       TEXT,

    -- 用于与链路关联（spec §8.6）
    trace_id          TEXT,

    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- spec §8.6 要求取值受限；用 CHECK 在数据库层强制，不靠应用层自律
    CONSTRAINT audit_log_action_class_check CHECK (
        action_class IN (
            'tool_call','change_proposal','approval','execute','rollback',
            'fault_injection','business_operation','memory_write','report'
        )
    ),
    CONSTRAINT audit_log_outcome_check CHECK (
        outcome IS NULL OR outcome IN (
            'allowed','denied','approved','rejected','executed','failed'
        )
    ),
    CONSTRAINT audit_log_risk_level_check CHECK (
        risk_level IS NULL OR risk_level IN ('L0','L1','L2','L3','L4')
    ),
    -- actor 前缀规范（spec §8.6）
    CONSTRAINT audit_log_actor_prefix_check CHECK (
        actor LIKE 'agent:%' OR actor LIKE 'human:%' OR actor = 'system'
        OR actor LIKE 'scenario:%'
    )
);

CREATE INDEX IF NOT EXISTS idx_audit_log_time
    ON audit_log (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_incident
    ON audit_log (incident_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_actor
    ON audit_log (actor, occurred_at DESC);

-- ============================================================
--  3. approval_requests · 审批令牌
--     spec §8.6 / §2 H4 / §6.3
-- ============================================================
CREATE TABLE IF NOT EXISTS approval_requests (
    request_id        UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    incident_id       TEXT NOT NULL,

    -- ★ 审批绑定到具体动作，防"批准 A 却做 B"（spec §2 H4 / §6.3）
    action_hash       TEXT NOT NULL,

    proposed_action   JSONB NOT NULL,
    risk_level        TEXT NOT NULL,
    blast_radius      JSONB,
    dry_run_diff      TEXT,
    rollback_plan     TEXT,

    status            TEXT NOT NULL DEFAULT 'pending',

    requested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at        TIMESTAMPTZ,

    -- ★ H4 红线：审批人不能是 Agent 自己
    decided_by        TEXT,

    -- spec §6.3：15 分钟未响应即过期
    expires_at        TIMESTAMPTZ NOT NULL DEFAULT now() + interval '15 minutes',

    -- spec §8.6 硬性约束 1：status 取值受限
    CONSTRAINT approval_requests_status_check CHECK (
        status IN ('pending','approved','rejected','expired','executed')
    ),
    -- spec §8.6 硬性约束 2：禁止 agent: 前缀（H4 红线，数据库层强制）
    CONSTRAINT approval_requests_not_self_decided CHECK (
        decided_by IS NULL OR decided_by NOT LIKE 'agent:%'
    )
);

CREATE INDEX IF NOT EXISTS idx_approval_requests_status
    ON approval_requests (status, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_approval_requests_hash
    ON approval_requests (action_hash);

-- ============================================================
--  4. verification_methods · 验证方式取值（决策记录 A1 的落地点）
--
--  ★ 为什么需要这张表：
--    决策记录 A1 规定「验证方式的枚举**以数据库为准**」，原因是当时
--    文档用 human_verified、数据库用 verified_human，导致写入闸门的
--    权重字典键永远匹配不上库里的值 —— **不报错但完全失效**。
--
--    而 spec §8.6 同时规定「记忆层表（episodes 等）不在存储契约内，
--    属 Agent 实现内部事务」。两条要求叠加后出现一个空缺：
--    记忆层表尚未建立时，"以数据库为准"的那份依据就不存在了。
--    本表把该依据固化下来，使契约始终可被机器校验。
--
--  ★ 本表只固定"取值集合与权重"这一条契约；记忆层表的完整结构
--    由 Agent 实现在 T16 自行设计（spec §8.6 的留白）。
-- ============================================================
CREATE TABLE IF NOT EXISTS verification_methods (
    method      TEXT PRIMARY KEY,
    weight      REAL NOT NULL,
    description TEXT,

    -- 取值范围即契约本身（决策记录 A1）
    CONSTRAINT verification_methods_method_check CHECK (
        method IN ('verified_human', 'verified_metrics', 'unverified', 'rejected')
    ),
    -- 权重必须在 [0,1]；闸门 1 以 0.7 为界
    CONSTRAINT verification_methods_weight_check CHECK (
        weight >= 0.0 AND weight <= 1.0
    )
);

-- 权重来自 spec §3.3 写入闸门 1
INSERT INTO verification_methods (method, weight, description) VALUES
    ('verified_human',   1.00, '人工确认 —— 最强验证'),
    ('verified_metrics', 0.85, '指标恢复 —— 客观但可能有巧合'),
    ('unverified',       0.40, '未验证 —— 低于闸门阈值 0.7，拒绝写入长期记忆'),
    ('rejected',         0.00, '已被否决 —— 永不写入长期记忆')
ON CONFLICT (method) DO UPDATE
    SET weight = EXCLUDED.weight,
        description = EXCLUDED.description;


-- ============================================================
--  自检：确认契约本身成立
--  ★ 建表脚本自检，避免"以为建了其实没建"
-- ============================================================
DO $$
DECLARE
    missing_tables TEXT[] := '{}';
    t              TEXT;
    required       TEXT[] := ARRAY[
        'change_events', 'audit_log', 'approval_requests', 'verification_methods'
    ];
BEGIN
    FOREACH t IN ARRAY required LOOP
        IF to_regclass('public.' || t) IS NULL THEN
            missing_tables := array_append(missing_tables, t);
        END IF;
    END LOOP;

    IF array_length(missing_tables, 1) > 0 THEN
        RAISE EXCEPTION '存储契约未建立，缺少表: %', missing_tables;
    END IF;

    -- 验证方式取值必须与决策记录 A1 一致（4 个，不多不少）
    IF (SELECT count(*) FROM verification_methods) <> 4 THEN
        RAISE EXCEPTION 'verification_methods 应有 4 行，实际 % 行',
            (SELECT count(*) FROM verification_methods);
    END IF;

    RAISE NOTICE '==============================================';
    RAISE NOTICE ' 存储契约已就绪（依据 spec.md §8.6）';
    RAISE NOTICE '   change_events        : % 行', (SELECT count(*) FROM change_events);
    RAISE NOTICE '   audit_log            : % 行', (SELECT count(*) FROM audit_log);
    RAISE NOTICE '   approval_requests    : % 行', (SELECT count(*) FROM approval_requests);
    RAISE NOTICE '   verification_methods : % 行（决策记录 A1 的取值依据）',
        (SELECT count(*) FROM verification_methods);
    RAISE NOTICE ' 说明：记忆层表（episodes/procedures/services/';
    RAISE NOTICE '       service_dependencies）不在契约内，属 Agent 实现。';
    RAISE NOTICE '==============================================';
END $$;
