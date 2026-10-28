"""Generic SDK access hypotheses; access retries themselves never satisfy cause requirements."""
SPECS={
 'sdk_process_unavailable':('application_error','目标实例未运行或未在登记地址监听',('sdk_process_not_listening_at_failure',)),
 'sdk_network_path':('downstream_latency','AIOps 到 SDK 的网络路径存在路由、解析或隔离故障',('sdk_network_path_failed_at_failure',)),
 'sdk_handler_blocked':('pool_contention','目标 SDK 请求处理被阻塞或资源等待拖延',('sdk_runtime_blocked_at_failure',)),
 'sdk_target_configuration':('change_induced','登记地址或申报接口与运行期配置不一致',('sdk_registered_target_mismatch',)),
 'sdk_permission_configuration':('change_induced','SDK 访问凭据或读取权限与运行期配置不一致',('sdk_read_permission_mismatch',)),
 'sdk_observer_egress':('application_error','AIOps 观测方自身的 DNS、连接或网络出口异常',('aiops_observer_egress_failed',)),
 'sdk_interface_handler':('application_error','SDK 只读接口自身处理错误，不能由此推定业务接口失败',('sdk_handler_failure_at_access',)),
}
QUESTIONS={
 'sdk_process_not_listening_at_failure':'同实例/boot、失败时刻的进程/容器退出记录与端口监听状态是否见证 SDK 不可用',
 'sdk_network_path_failed_at_failure':'从指定 AIOps 观测方到该地址的同时间 DNS、路由、连接探测或防火墙记录是否见证路径故障',
 'sdk_runtime_blocked_at_failure':'同实例、失败时刻的运行栈、CPU/工作池/事件循环等待记录是否见证接口处理阻塞',
 'sdk_registered_target_mismatch':'部署清单、实际监听地址及 SDK 路由配置是否与登记版本不一致',
 'sdk_read_permission_mismatch':'脱敏权限/凭据版本与 SDK 鉴权拒绝日志是否见证同一次访问权限不匹配（无需凭据原文）',
 'aiops_observer_egress_failed':'该 AIOps 观测方同时间的 DNS/网络出口或平台错误记录是否见证观察侧故障',
 'sdk_handler_failure_at_access':'同实例 SDK 异常栈与访问请求标识是否独立见证该接口处理错误',
}


def keys(code):
    suffix=code.removeprefix('SDK_ACCESS_')
    if suffix in ('UNAUTHORIZED','FORBIDDEN'):return ['sdk_permission_configuration','sdk_target_configuration']
    if suffix in ('NOT_FOUND','INVALID_PARAMS','UNAVAILABLE'):return ['sdk_target_configuration','sdk_interface_handler']
    if suffix in ('HTTP_5XX','RATE_LIMITED'):return ['sdk_interface_handler','sdk_handler_blocked']
    if suffix=='READ_TIMEOUT':return ['sdk_handler_blocked','sdk_network_path','sdk_observer_egress']
    return ['sdk_process_unavailable','sdk_network_path','sdk_observer_egress']
