-- ============================================================
--  存储契约 · 数据库角色与权限（严格依据 spec.md §8.6）
--
--  spec §8.6「数据库角色与权限契约」：
--    aiops         → 全部（建表与迁移）
--    aiops_runner  → SELECT 全部表；INSERT 到 change_events/audit_log；
--                    显式 REVOKE UPDATE, DELETE, TRUNCATE 于上述两表
--
--  ★ 为什么必须 REVOKE（spec §8.6 原文）：
--    spec §2 H3/H4 与 §3.4 要求**审计不可篡改**。
--    若运行角色能改写 change_events，就等于能伪造证据 ——
--    变更归因的证据链失去意义。
--
--  ★ spec §8.6 强调：「权限必须用代码断言（has_table_privilege 返回 false），
--    不能只靠『我记得授过权』」。故本文件末尾做强制自检。
-- ============================================================

-- ------------------------------------------------------------
--  1. 创建运行角色
-- ------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aiops_runner') THEN
        CREATE ROLE aiops_runner LOGIN PASSWORD 'runner_dev_pw';
        RAISE NOTICE '已创建角色 aiops_runner';
    ELSE
        RAISE NOTICE '角色 aiops_runner 已存在，跳过创建';
    END IF;
END $$;

-- ------------------------------------------------------------
--  2. 基础权限
-- ------------------------------------------------------------
GRANT CONNECT ON DATABASE aiops TO aiops_runner;
GRANT USAGE ON SCHEMA public TO aiops_runner;

-- spec §8.6：SELECT 全部表
GRANT SELECT ON ALL TABLES IN SCHEMA public TO aiops_runner;

-- spec §8.6：INSERT 到 change_events / audit_log
GRANT INSERT ON change_events TO aiops_runner;
GRANT INSERT ON audit_log     TO aiops_runner;

-- 审批令牌：运行角色只读（写入与决策由服务端 aiops 角色负责）
GRANT SELECT ON approval_requests TO aiops_runner;

-- 序列（audit_log 用 BIGSERIAL，INSERT 需要推进序列）
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO aiops_runner;

-- ------------------------------------------------------------
--  3. ★ 显式收权：确保审计不可篡改（spec §8.6 核心要求）
-- ------------------------------------------------------------
REVOKE UPDATE, DELETE, TRUNCATE ON change_events FROM aiops_runner;
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log     FROM aiops_runner;

-- 审批令牌不得被运行角色改写（否则可伪造"已审批"）
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON approval_requests FROM aiops_runner;

-- 未来新建的表自动继承 SELECT（避免遗漏授权导致运行期失败）
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO aiops_runner;

-- ------------------------------------------------------------
--  4. ★ 强制自检（spec §8.6 明确要求用代码断言）
--
--     任一项不满足即 RAISE EXCEPTION，让初始化直接失败 ——
--     而不是"启动成功但审计可被篡改"。
-- ------------------------------------------------------------
DO $$
DECLARE
    can_update_change   BOOLEAN;
    can_delete_change   BOOLEAN;
    can_update_audit    BOOLEAN;
    can_delete_audit    BOOLEAN;
    can_insert_change   BOOLEAN;
    can_select_change   BOOLEAN;
    can_write_approval  BOOLEAN;
BEGIN
    SELECT has_table_privilege('aiops_runner', 'change_events', 'UPDATE') INTO can_update_change;
    SELECT has_table_privilege('aiops_runner', 'change_events', 'DELETE') INTO can_delete_change;
    SELECT has_table_privilege('aiops_runner', 'audit_log',     'UPDATE') INTO can_update_audit;
    SELECT has_table_privilege('aiops_runner', 'audit_log',     'DELETE') INTO can_delete_audit;
    SELECT has_table_privilege('aiops_runner', 'change_events', 'INSERT') INTO can_insert_change;
    SELECT has_table_privilege('aiops_runner', 'change_events', 'SELECT') INTO can_select_change;
    SELECT has_table_privilege('aiops_runner', 'approval_requests', 'INSERT') INTO can_write_approval;

    -- 必须为 false 的四项（审计不可篡改）
    IF can_update_change THEN
        RAISE EXCEPTION '权限契约违规：aiops_runner 不应有 change_events 的 UPDATE 权限';
    END IF;
    IF can_delete_change THEN
        RAISE EXCEPTION '权限契约违规：aiops_runner 不应有 change_events 的 DELETE 权限';
    END IF;
    IF can_update_audit THEN
        RAISE EXCEPTION '权限契约违规：aiops_runner 不应有 audit_log 的 UPDATE 权限';
    END IF;
    IF can_delete_audit THEN
        RAISE EXCEPTION '权限契约违规：aiops_runner 不应有 audit_log 的 DELETE 权限';
    END IF;

    -- 必须为 true 的三项（运行角色需要写入变更与审计）
    IF NOT can_insert_change THEN
        RAISE EXCEPTION '权限契约违规：aiops_runner 缺少 change_events 的 INSERT 权限';
    END IF;
    IF NOT can_select_change THEN
        RAISE EXCEPTION '权限契约违规：aiops_runner 缺少 change_events 的 SELECT 权限';
    END IF;

    -- 审批令牌不得可写
    IF can_write_approval THEN
        RAISE EXCEPTION '权限契约违规：aiops_runner 不应能写入 approval_requests';
    END IF;

    RAISE NOTICE '==============================================';
    RAISE NOTICE ' 角色与权限契约校验通过（spec §8.6）';
    RAISE NOTICE '   aiops_runner → change_events  INSERT : %', can_insert_change;
    RAISE NOTICE '   aiops_runner → change_events  SELECT : %', can_select_change;
    RAISE NOTICE '   aiops_runner → change_events  UPDATE : %  (须为 false)', can_update_change;
    RAISE NOTICE '   aiops_runner → audit_log      UPDATE : %  (须为 false)', can_update_audit;
    RAISE NOTICE '   aiops_runner → approval_requests INSERT: %  (须为 false)', can_write_approval;
    RAISE NOTICE '==============================================';
END $$;
