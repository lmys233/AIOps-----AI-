"""常见机制的起始候选；它们仅待验证，其他异常由模型补充。"""
import re
from .contracts import Candidate, Problem, Requirement
from .facts import stable_id

# 条件是命名的只读事实，异常日志只作为提出依据，不替代这些验证。
SPECS = {
    'migration_missing': ('change_induced', '缺失的数据库对象迁移未执行',
        ('runtime_target_verified','object_absent','migration_not_applied')),
    'target_mismatch': ('change_induced', '应用连接目标或 schema 解析路径与预期不一致',
        ('runtime_target_mismatch',)),
    'object_removed': ('change_induced', '对象在故障之前被删除或改名',
        ('runtime_target_verified','ddl_removed_before_fault')),
    'worker_database_wait': ('pool_contention', 'worker 被数据库连接等待占用，使工作池无法接新任务',
        ('worker_database_wait','pool_pressure')),
    'worker_downstream_wait': ('downstream_latency', 'worker 被下游调用阻塞占用',
        ('worker_downstream_wait','correlated_downstream_wait')),
    'worker_capacity_mismatch': ('capacity_surge', '工作池承载配置与实际入队负载不匹配',
        ('worker_capacity_mismatch',)),
    'lock_order_conflict': ('pool_contention', '事务以冲突的顺序获取相同锁，形成等待环',
        ('conflicting_lock_order','transaction_wait_cycle')),
    'long_transaction': ('pool_contention', '长事务持锁参与该次死锁等待环',
        ('long_transaction_holds_lock','transaction_wait_cycle')),
    'concurrent_duplicate_work': ('capacity_surge', '重复并发处理争用相同锁对象',
        ('duplicate_work_same_lock','transaction_wait_cycle')),
}
TOOLS = {'object_absent':'db_object_read','runtime_target_verified':'db_context_read',
         'runtime_target_mismatch':'db_context_read','migration_not_applied':'migration_read',
         'ddl_removed_before_fault':'ddl_audit_read'}
SPECS.update(http_database_failure=('application_error','数据库错误传递到同一请求并导致接口失败',
    ('same_request_database_failure',)),sql_lock_wait=('pool_contention','SQL 执行期间存在数据库锁等待',
    ('query_blocked_by_lock',)))
SPECS.update(pool_database_holders=('pool_contention','连接持有期间的 SQL 执行占满池，阻塞新连接获取',
    ('pool_full_during_timeout','holder_sql_spans_timeout')),
    pool_missing_release=('pool_contention','连接长期未归还，持续占用池容量',
    ('complete_holder_lifecycle','return_obligation_violated')),
    request_database_latency=('pool_contention','同次请求的数据库等待构成主要慢阶段',
    ('same_request_database_dominant',)),
    request_downstream_latency=('downstream_latency','同次请求的下游调用构成主要慢阶段',
    ('same_request_downstream_dominant',)),
    plan_regression=('change_induced','可比执行条件下查询计划变化伴随 SQL 耗时退化',
    ('comparable_plan_regression',)),
    process_platform_oom=('application_error','平台明确记录内存限制导致该次应用终止',
    ('platform_oom_matches_exit',)),
    process_external_signal=('application_error','外部终止事件与该次进程退出匹配',
    ('external_signal_matches_exit',)))
TOOLS.update(conflicting_lock_order='db_deadlock_read',transaction_wait_cycle='db_deadlock_read',
    long_transaction_holds_lock='db_activity_read',duplicate_work_same_lock='db_deadlock_read',
    worker_database_wait='runtime_snapshot_read',pool_pressure='connection_holders_read',
    worker_downstream_wait='runtime_snapshot_read',correlated_downstream_wait='runtime_snapshot_read',
    worker_capacity_mismatch='worker_state_read',same_request_database_failure='runtime_snapshot_read',
    query_blocked_by_lock='db_activity_read')
TOOLS.update(pool_full_during_timeout='connection_holders_read',holder_sql_spans_timeout='db_activity_read',
    complete_holder_lifecycle='connection_holders_read',return_obligation_violated='connection_holders_read',
    same_request_database_dominant='runtime_snapshot_read',same_request_downstream_dominant='runtime_snapshot_read',
    comparable_plan_regression='db_plan_read',platform_oom_matches_exit='process_event_read',
    external_signal_matches_exit='process_event_read')
SPECS.update(request_client_disconnect=('application_error','运行框架在观测客户端断开后取消了同一次请求',
    ('client_disconnect_cancelled_request',)),
    request_configured_deadline=('application_error','已配置的运行期限触发并中止了同一次请求',
    ('configured_deadline_aborted_request',)),
    request_shutdown_cancel=('application_error','运行框架在停机取消分支中取消了同一次请求',
    ('framework_shutdown_cancelled_request',)))
TOOLS.update({key:'request_cancellation_read' for key in ('client_disconnect_cancelled_request',
    'configured_deadline_aborted_request','framework_shutdown_cancelled_request')})
from ...observed.access_candidates import SPECS as SDK_SPECS,keys as sdk_keys
SPECS.update(SDK_SPECS)
TOOLS.update({signal:'runtime_snapshot_read' if signal=='sdk_runtime_blocked_at_failure' else 'process_event_read'
    for spec in SDK_SPECS.values() for signal in spec[2]})


def requirement(key: str, problem: Problem) -> Requirement:
    params={'service':problem.service, 'object_name':problem.object_name}
    if TOOLS.get(key) not in ('db_object_read','db_context_read','migration_read','ddl_audit_read',None):
        params.update(env=problem.env,start=problem.time_range[0].isoformat(),end=problem.time_range[1].isoformat())
        if problem.cluster:params['cluster']=problem.cluster
        if problem.request_id:
            params['request_id']=problem.request_id
            if problem.instance:params['instance']=problem.instance
    from ...diagnostics.questions import QUESTIONS
    return Requirement(key=key, question=f'核查 {problem.service}：'+QUESTIONS.get(key,key), signal=key,
        predicate='equals', expected=True, tool=TOOLS.get(key, ''),
        params=params)


def seeds(problem: Problem) -> list[Candidate]:
    if problem.error_type.startswith('SDK_ACCESS_'):keys=sdk_keys(problem.error_type)
    elif problem.error_type in ('HTTP_REQUEST_CANCELLED','HTTP_REQUEST_TIMEOUT'):
        keys=['request_client_disconnect','request_configured_deadline','request_shutdown_cancel']
    elif problem.error_type=='HTTP_REQUEST_FAILED':keys=['http_database_failure']
    elif problem.error_type=='REQUEST_SLOW':keys=['request_database_latency','request_downstream_latency']
    elif problem.error_type=='SQL_SLOW':keys=['sql_lock_wait','plan_regression']
    elif problem.error_type=='POOL_ACQUIRE_TIMEOUT':keys=['pool_database_holders','pool_missing_release']
    elif problem.error_type in ('PROCESS_EXIT','PROCESS_START_FAILED','PROCESS_RESTART_BURST'):
        keys=['process_platform_oom','process_external_signal']
    elif problem.object_name and re.search(r'\b(?:relation|table)\b.+does not exist',
                                         ' '.join(problem.samples),re.I):
        keys = ['migration_missing','target_mismatch','object_removed']
    elif problem.error_type == 'THREAD_POOL_REJECTED':
        keys = ['worker_database_wait','worker_downstream_wait','worker_capacity_mismatch']
    elif problem.error_type == 'DB_DEADLOCK':
        keys = ['lock_order_conflict','long_transaction','concurrent_duplicate_work']
    else:
        keys = []
    return [Candidate(candidate_id=stable_id('H',problem.problem_id,key),
        problem_id=problem.problem_id, mechanism_key=key, category=SPECS[key][0],
        statement=SPECS[key][1], basis_ids=problem.evidence_ids,
        requirements=[requirement(k, problem) for k in SPECS[key][2]]) for key in keys]
