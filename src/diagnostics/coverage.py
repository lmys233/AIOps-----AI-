"""Open diagnostic scope; these eight initial families are acceptance targets."""
FAMILIES = {
    'http_failure': dict(fact='请求失败', mechanism='自身/下游/业务错误',
        sources=['sdk_http','logs','traces','changes'],
        requirements=['same_request_error','origin_verified'],
        baseline='部分发现；完整异常与同请求关联待补'),
    'request_latency': dict(fact='请求慢或超时', mechanism='排队/查询/下游/执行阶段',
        sources=['sdk_http','sdk_pool','sdk_sql','sdk_worker','traces'],
        requirements=['same_request_phase','overlap_accounted'],
        baseline='部分延迟与慢链路；请求阶段待补'),
    'object_missing': dict(fact='数据库无法解析对象', mechanism='对象在解析路径不可见',
        sources=['sdk_sql','database_observer','migration','ddl_audit'],
        requirements=['runtime_target_verified','object_absent','historical_change_verified'],
        baseline='事实/直接机制及目录读取；运行身份和历史来源缺失'),
    'pool_timeout': dict(fact='获取连接失败/超时', mechanism='等待或持有占满池',
        sources=['sdk_pool','sdk_sql','database_observer'],
        requirements=['pool_pressure','correlated_holder','holder_wait_verified'],
        baseline='池指标/超时日志；持有任务关联待补'),
    'deadlock': dict(fact='死锁/锁等待', mechanism='事务等待环',
        sources=['sdk_transaction','database_observer','database_log'],
        requirements=['transaction_wait_cycle','conflicting_lock_order'],
        baseline='死锁报错/机制；锁与事务来源未接入'),
    'worker_rejection': dict(fact='工作池满/拒绝任务', mechanism='有界池承载饱和',
        sources=['sdk_worker','sdk_runtime','sdk_pool','traces'],
        requirements=['worker_occupied','correlated_wait','capacity_verified'],
        baseline='拒绝报错/机制；工作池与栈来源未接入'),
    'slow_query': dict(fact='SQL执行慢', mechanism='执行/锁等待/计划变化',
        sources=['sdk_sql','database_observer','database_log','query_plan'],
        requirements=['query_execution_slow','comparable_plan','plan_changed'],
        baseline='专项采集与验证尚未交付'),
    'process_exit': dict(fact='退出/重启', mechanism='异常终止或正常结束',
        sources=['sdk_lifecycle','process_observer','platform_event'],
        requirements=['independent_exit_record','termination_reason_verified'],
        baseline='专项采集与独立退出来源尚未交付'),
}
SEMANTICS = {
    'pool_waiting': dict(unit='connections', kind='gauge', max_labels=128),
    'pool_in_use': dict(unit='connections', kind='gauge', max_labels=128),
    'pool_acquire': dict(unit='seconds', kind='histogram', max_labels=128),
    'connection_hold': dict(unit='seconds', kind='histogram', max_labels=128),
    'query_execution': dict(unit='seconds', kind='histogram', max_labels=128),
    'worker_wait': dict(unit='seconds', kind='histogram', max_labels=128),
    'http_latency': dict(unit='seconds', kind='histogram', max_labels=1024),
    'http_failures': dict(unit='requests', kind='counter', max_labels=1024),
}


def usable(row):
    """A placeholder zero is never evidence of health."""
    return row['quality']['status'] == 'available'
