"""Archive observation to blackboard; descriptive facts, never inferred causes."""
import hashlib
import json
import os
from diagnostics.contract import timestamp
from ..common.contracts import Evidence,EntityRef


def fault(row):
    kind=row['kind'];p=row['payload']
    if row['quality']['status'] not in ('available','partial'):return None
    if kind=='sdk_access' and p.get('sent') and not p.get('ok'):
        interface={'snapshot':'/_aiops/snapshot','capabilities':'/_aiops/capabilities','health':'/health',
            'probe_observations':'/_aiops/probe-observations'}.get(p['interface_id'],'/registered-interface')
        return 'SDK_ACCESS_'+p['error'].upper(),f"AIOps 观测方 {p['observer_id']} 访问 SDK GET {interface} 失败：{p['error']}；尝试 {p['attempt']}，{p['elapsed_ms']:.1f}ms；不等于业务宕机"
    if kind=='sql_error':
        code='DB_DEADLOCK' if p.get('sqlstate')=='40P01' else 'POOL_QUERY_FAILED'
        return code,p.get('message') or p.get('exception_type','SQL query failed')
    if kind=='pool_timeout':return 'POOL_ACQUIRE_TIMEOUT','等待数据库连接超过期限；'+json.dumps(p,ensure_ascii=False)
    if kind=='worker_reject':
        full=(p.get('reason')=='capacity' and p.get('capacity',0)>0 and p.get('active',0)>=p['capacity'] and
              p.get('queue_capacity') is not None and max(0,p.get('queued',0)-1)>=p['queue_capacity'])
        return ('THREAD_POOL_REJECTED' if full else 'WORKER_SUBMIT_FAILED'), '工作任务被拒绝；'+json.dumps(p,ensure_ascii=False)
    if kind=='http_request':
        if p.get('outcome') in ('post_response_cancelled','post_response_timeout'):return None
        route=f"{p.get('method','')} {p.get('route','')}"
        if p.get('outcome')=='cancelled':return 'HTTP_REQUEST_CANCELLED',route+' 请求被取消，取消来源待核实'
        if p.get('outcome')=='timeout' or p.get('status')==504:
            return 'HTTP_REQUEST_TIMEOUT',route+' 出现超时异常或超时响应，配置期限与阶段待核实'
        if p.get('outcome') in ('exception','server_error') or isinstance(p.get('status'),int) and p['status']>=500:
            return 'HTTP_REQUEST_FAILED',f"{p.get('method','')} {p.get('route','')} 失败："+p.get('message',str(p.get('status')))
        if p.get('total_ms',0)>float(os.getenv('AIOPS_DIAGNOSTIC_HTTP_SLOW_MS','1000')):
            return 'REQUEST_SLOW',f"{p.get('method','')} {p.get('route','')} 请求耗时 {p['total_ms']:.1f}ms 超过诊断门槛（不是自动认定 SLO 违约）"
    if kind=='sql_query' and p.get('purpose','business')=='business' and p.get('execution_ms',0)>float(os.getenv('AIOPS_DIAGNOSTIC_SQL_SLOW_MS','500')):
        return 'SQL_SLOW',f"SQL 指纹 {p.get('query_fingerprint','unknown')} 执行 {p['execution_ms']:.1f}ms；获取连接时间另计"
    if kind in ('process_start_failed','sdk_start_failed'):
        return 'PROCESS_START_FAILED',('独立观察器' if kind=='process_start_failed' else 'SDK')+'记录应用启动失败：'+p.get('exception_type','unknown')
    if kind=='process_restart_burst':return 'PROCESS_RESTART_BURST',f"独立观察器在 {p['interval_seconds']} 秒内记录 {p['starts']} 次不同启动；原因待核实"
    if kind=='process_exit' and p.get('exit_code') not in (None,0):
        return 'PROCESS_EXIT',f"独立观察器记录应用退出，exit_code={p['exit_code']}；原因={p.get('termination_reason','unknown')}"
    return None


def from_event(row,role,raw_ref,window=None):
    i=row['identity'];code_sample=fault(row)
    if row['correlation'].get('traffic_origin')=='active_probe' and code_sample:
        code_sample=(code_sample[0],'[主动探测样本] '+code_sample[1])
    start,end=(timestamp(row['coverage'][k]) for k in ('start','end'))
    if window is not None:start,end=min(start,window[0]),max(end,window[1])
    at=timestamp(row['occurred_at']);at=min(end,max(start,at))
    tags=['diagnostic_event_json='+json.dumps(row,ensure_ascii=False),
          'observation_id='+row['observation_id'],'diagnostic_source='+row['source']]
    for key,value in row['correlation'].items():tags.append(key+'='+value)
    if code_sample:tags+=['error_type='+code_sample[0],'sample='+code_sample[1][:12000]]
    key=hashlib.sha256((role+i.get('observed_id','')+row['event_id']).encode()).hexdigest()[:24]
    entity=EntityRef.make('service',i['service'],cluster=i['cluster'])
    if i.get('observed_id'):entity=entity.model_copy(update={'observed_id':i['observed_id']})
    return Evidence(evidence_id='D-'+key,source='logs' if code_sample else 'changes',agent_role=role,
        entity=entity,
        signal='log_error_burst' if code_sample else 'diagnostic_observation',direction='spike' if code_sample else 'flat',
        magnitude=1,time_range=(start,end),onset=at,confidence=1,raw_ref=raw_ref,
        summary=code_sample[1][:1000] if code_sample else row['kind']+'（结构化来源与覆盖已留存）',tags=tags)


def events_of(evidence):
    rows=[];seen=set()
    for item in evidence:
        encoded=next((t.split('=',1)[1] for t in item.tags if t.startswith('diagnostic_event_json=')),None)
        if encoded:
            row=json.loads(encoded)
            key=(row['observation_id'],row['kind'])
            if key not in seen:rows.append(row);seen.add(key)
    return rows
